# NWO Robotics API - Render Deployment
# FastAPI fallback for nwo.capital PHP APIs
# v2.2.0 — fixes:
#   - wallet now read from X-NWO-Wallet header (body fallback kept for compat)
#   - DB_PATH defaults to /data/nwo_api.db (Render persistent disk at /data)
#   - key_preview field added to GET /api-keys response
#   - robots GET returns both id and robot_id fields for frontend compat
#   - chat endpoint reads robot_id from body, wallet from header
#   - POST /api-keys/validate added — used by Cloudflare Worker sim key check
#   - CORS updated: nwo.ciprianpater.workers.dev + *.workers.dev added

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
import sqlite3
import json
import hashlib
import secrets
from datetime import datetime
import os

app = FastAPI(
    title="NWO Robotics API",
    description="FastAPI fallback for NWO Robotics ecosystem",
    version="2.2.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nwo.capital",
        "https://CPater-nwo-capital.hf.space",
        "https://*.hf.space",
        "https://nwo.ciprianpater.workers.dev",   # Cloudflare Worker (sim key validation)
        "https://*.workers.dev",                   # any future Workers on this account
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database ──────────────────────────────────────────────────────────────────
# Default: /data/nwo_api.db  (Render persistent disk, mounted at /data)
# Set DATABASE_URL=/data/nwo_api.db in Render environment variables.
DB_PATH = os.getenv("DATABASE_URL", "/data/nwo_api.db")


def init_db():
    """Initialize SQLite database with all tables."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # WAL mode for better concurrent read/write
    cursor.execute("PRAGMA journal_mode=WAL")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id        TEXT UNIQUE NOT NULL,
            wallet        TEXT NOT NULL,
            name          TEXT NOT NULL,
            api_key_hash  TEXT NOT NULL,
            key_prefix    TEXT NOT NULL,
            key_suffix    TEXT NOT NULL,
            usage_count   INTEGER DEFAULT 0,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used     TIMESTAMP,
            is_active     BOOLEAN DEFAULT 1
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet     TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS model_usage (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet     TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            calls      INTEGER DEFAULT 0,
            cost_eth   REAL DEFAULT 0.0,
            last_used  TIMESTAMP,
            UNIQUE(wallet, model_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS iot_networks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            network_id   TEXT UNIQUE NOT NULL,
            wallet       TEXT NOT NULL,
            name         TEXT NOT NULL,
            status       TEXT DEFAULT 'active',
            device_count INTEGER DEFAULT 0,
            data_points  INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS robots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            robot_id      TEXT UNIQUE NOT NULL,
            wallet        TEXT NOT NULL,
            name          TEXT,
            type          TEXT DEFAULT 'other',
            description   TEXT,
            status        TEXT DEFAULT 'offline',
            agent_address TEXT,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen     TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS missions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id   TEXT UNIQUE NOT NULL,
            wallet       TEXT NOT NULL,
            robot_id     TEXT NOT NULL,
            type         TEXT NOT NULL,
            status       TEXT DEFAULT 'pending',
            waypoints    TEXT,
            earnings_eth REAL DEFAULT 0.0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    print(f"✅ Database initialized at {DB_PATH}")


@app.on_event("startup")
async def startup():
    init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8).upper()}"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def resolve_wallet(header_wallet: Optional[str], body_wallet: Optional[str]) -> str:
    """
    Prefer X-NWO-Wallet header (set by apiFetch with session signing).
    Fall back to wallet field in request body for backward compat.
    Raises 401 if neither is present.
    """
    wallet = (header_wallet or body_wallet or "").strip().lower()
    if not wallet:
        raise HTTPException(status_code=401, detail="Wallet not provided — connect MetaMask")
    return wallet


# ── Request / Response models ─────────────────────────────────────────────────

class ApiKeyCreate(BaseModel):
    name: str
    wallet: Optional[str] = None        # fallback; prefer X-NWO-Wallet header

class ApiKeyValidate(BaseModel):
    api_key: str                        # raw key as issued on creation
    wallet: Optional[str] = None        # if provided, ownership is also verified

class ChatMessage(BaseModel):
    message: str
    robot_id: Optional[str] = None
    wallet: Optional[str] = None
    history: Optional[List[Dict]] = []

class ModelUsage(BaseModel):
    model_id: str
    wallet: Optional[str] = None

class IoTNetworkCreate(BaseModel):
    name: str
    wallet: Optional[str] = None

class RobotCreate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = "other"
    description: Optional[str] = None
    agent_address: Optional[str] = None
    wallet: Optional[str] = None

class MissionCreate(BaseModel):
    robot_id: str
    type: str
    waypoints: Optional[List[Dict]] = []
    wallet: Optional[str] = None


# ── API Key endpoints ─────────────────────────────────────────────────────────

@app.post("/api-keys")
async def create_api_key(
    data: ApiKeyCreate,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    """Create a new API key scoped to the connected wallet."""
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)

    key_id   = generate_id("KEY")
    api_key  = "nwo_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    cursor = db.cursor()
    try:
        cursor.execute('''
            INSERT INTO api_keys
                (key_id, wallet, name, api_key_hash, key_prefix, key_suffix)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (key_id, wallet, data.name, key_hash, api_key[:12], api_key[-4:]))
        db.commit()
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    return {
        "key_id":      key_id,
        "id":          key_id,
        "name":        data.name,
        "api_key":     api_key,         # shown once — frontend stores in justCreated
        "key":         api_key,         # alias so data.key and data.api_key both work
        "key_prefix":  api_key[:12],
        "key_preview": api_key[:12] + "…" + api_key[-4:],
        "created_at":  datetime.now().isoformat(),
    }


@app.get("/api-keys")
async def list_api_keys(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,       # query-param fallback
):
    """List active API keys for the connected wallet (full keys never returned)."""
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute('''
        SELECT key_id, name, key_prefix, key_suffix, usage_count, created_at, is_active
        FROM api_keys
        WHERE wallet = ? AND is_active = 1
        ORDER BY created_at DESC
    ''', (resolved,))

    keys = []
    for row in cursor.fetchall():
        keys.append({
            "id":          row["key_id"],
            "key_id":      row["key_id"],
            "name":        row["name"],
            "key_prefix":  row["key_prefix"],
            "key_suffix":  row["key_suffix"],
            "key_preview": row["key_prefix"] + "…" + row["key_suffix"],
            "usage_count": row["usage_count"],
            "created_at":  row["created_at"],
            "is_active":   bool(row["is_active"]),
        })

    return {"status": "success", "count": len(keys), "keys": keys}


@app.post("/api-keys/validate")
async def validate_api_key(
    data: ApiKeyValidate,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Validate a raw API key submitted by a Cloudflare Worker or agent.
    Called by nwo.ciprianpater.workers.dev when an agent attaches a SIM key.

    - Hashes the submitted key and looks it up in the DB.
    - If `wallet` is provided in the body, also verifies ownership.
    - Bumps usage_count and last_used on success.
    - Never returns the hash or the full key — only a masked preview.
    """
    if not data.api_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    key_hash = hashlib.sha256(data.api_key.encode()).hexdigest()

    cursor = db.cursor()
    cursor.execute('''
        SELECT key_id, wallet, name, key_prefix, key_suffix, usage_count
        FROM api_keys
        WHERE api_key_hash = ? AND is_active = 1
    ''', (key_hash,))
    row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    # Optional wallet ownership check
    if data.wallet and row["wallet"] != data.wallet.strip().lower():
        raise HTTPException(
            status_code=403,
            detail="Key does not belong to the provided wallet"
        )

    # Bump usage
    cursor.execute('''
        UPDATE api_keys
        SET usage_count = usage_count + 1, last_used = ?
        WHERE key_id = ?
    ''', (datetime.now().isoformat(), row["key_id"]))
    db.commit()

    return {
        "valid":       True,
        "key_id":      row["key_id"],
        "name":        row["name"],
        "key_preview": row["key_prefix"] + "…" + row["key_suffix"],
        "wallet":      row["wallet"],
        "usage_count": row["usage_count"] + 1,
    }


@app.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
):
    """Soft-delete (revoke) an API key."""
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute('''
        UPDATE api_keys SET is_active = 0
        WHERE key_id = ? AND wallet = ?
    ''', (key_id, resolved))
    db.commit()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Key not found or not owned by this wallet")

    return {"status": "success", "message": "Key revoked"}


# ── Chat endpoints ────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(
    data: ChatMessage,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)

    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO chat_messages (wallet, role, content) VALUES (?, 'user', ?)",
        (wallet, data.message)
    )

    responses = {
        "create":   "Go to Dashboard and click '+ Register Robot' to register a robot.",
        "fleet":    "Navigate to the Tracking tab to view your registered fleet.",
        "navigate": "Provide destination coordinates and I'll route the command.",
        "help":     "Available: create robot, view fleet, navigate, check status, earnings.",
        "earnings": "Earnings split: 35% Guardian / 35% Savings / 30% Operational.",
        "status":   "Connect your wallet and visit Tracking to see live robot status.",
    }

    ai_response = "I'm here to help with NWO Robotics. Ask about creating robots, viewing your fleet, or navigation."
    for kw, reply in responses.items():
        if kw in data.message.lower():
            ai_response = reply
            break

    if data.robot_id:
        ai_response = f"[robot:{data.robot_id}] " + ai_response

    cursor.execute(
        "INSERT INTO chat_messages (wallet, role, content) VALUES (?, 'assistant', ?)",
        (wallet, ai_response)
    )
    db.commit()

    return {
        "status":    "success",
        "response":  ai_response,
        "reply":     ai_response,       # alias — frontend checks both
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/chat/history")
async def get_chat_history(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
    limit: int = 50,
):
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute('''
        SELECT role, content, timestamp FROM chat_messages
        WHERE wallet = ? ORDER BY timestamp DESC LIMIT ?
    ''', (resolved, limit))

    messages = [
        {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
        for r in cursor.fetchall()
    ]
    return {"status": "success", "count": len(messages), "messages": list(reversed(messages))}


# ── Model usage endpoints ─────────────────────────────────────────────────────

@app.get("/model-usage")
async def get_model_usage(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
):
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute(
        "SELECT model_id, calls, cost_eth, last_used FROM model_usage WHERE wallet = ?",
        (resolved,)
    )

    models = []
    total_calls, total_cost = 0, 0.0
    for row in cursor.fetchall():
        models.append({
            "model_id":  row["model_id"],
            "calls":     row["calls"],
            "cost_eth":  row["cost_eth"],
            "last_used": row["last_used"],
        })
        total_calls += row["calls"]
        total_cost  += row["cost_eth"]

    return {
        "status": "success",
        "wallet": resolved,
        "usage":  {"calls": total_calls, "cost": total_cost, "models": models},
    }


@app.post("/model-usage/track")
async def track_model_usage(
    data: ModelUsage,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)

    model_costs = {
        "gpt-4":    0.0001,
        "claude-3": 0.00015,
        "llama-3":  0.00005,
        "timesfm":  0.00002,
    }
    cost = model_costs.get(data.model_id, 0.0001)
    now  = datetime.now().isoformat()

    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO model_usage (wallet, model_id, calls, cost_eth, last_used)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(wallet, model_id) DO UPDATE SET
            calls     = calls + 1,
            cost_eth  = cost_eth + ?,
            last_used = ?
    ''', (wallet, data.model_id, cost, now, cost, now))
    db.commit()

    return {"status": "success", "message": "Usage tracked"}


# ── IoT network endpoints ─────────────────────────────────────────────────────

@app.post("/iot-networks")
async def create_iot_network(
    data: IoTNetworkCreate,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    wallet     = resolve_wallet(x_nwo_wallet, data.wallet)
    network_id = generate_id("NET")

    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO iot_networks (network_id, wallet, name) VALUES (?, ?, ?)",
        (network_id, wallet, data.name)
    )
    db.commit()

    return {"status": "success", "network_id": network_id, "name": data.name}


@app.get("/iot-networks")
async def list_iot_networks(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
):
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute('''
        SELECT network_id, name, status, device_count, data_points, created_at
        FROM iot_networks WHERE wallet = ? ORDER BY created_at DESC
    ''', (resolved,))

    networks = [
        {
            "id":           r["network_id"],
            "name":         r["name"],
            "status":       r["status"],
            "device_count": r["device_count"],
            "data_points":  r["data_points"],
            "created_at":   r["created_at"],
        }
        for r in cursor.fetchall()
    ]
    return {"status": "success", "count": len(networks), "networks": networks}


# ── Robot endpoints ───────────────────────────────────────────────────────────

@app.post("/robots")
async def create_robot(
    data: RobotCreate,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    wallet   = resolve_wallet(x_nwo_wallet, data.wallet)
    robot_id = generate_id("ROB")
    name     = data.name or f"Robot {robot_id[-4:]}"

    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO robots (robot_id, wallet, name, type, description, agent_address)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (robot_id, wallet, name, data.type or "other", data.description, data.agent_address))
    db.commit()

    return {
        "status":   "success",
        "id":       robot_id,
        "robot_id": robot_id,
        "name":     name,
        "message":  "Robot registered",
    }


@app.get("/robots")
async def list_robots(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
):
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    cursor.execute('''
        SELECT robot_id, name, type, description, status, agent_address, created_at, last_seen
        FROM robots WHERE wallet = ? ORDER BY created_at DESC
    ''', (resolved,))

    robots = [
        {
            "id":            r["robot_id"],
            "robot_id":      r["robot_id"],
            "name":          r["name"],
            "type":          r["type"],
            "description":   r["description"],
            "status":        r["status"],
            "agent_address": r["agent_address"],
            "created_at":    r["created_at"],
            "last_seen":     r["last_seen"],
        }
        for r in cursor.fetchall()
    ]
    return {"status": "success", "count": len(robots), "robots": robots}


# ── Mission endpoints ─────────────────────────────────────────────────────────

@app.post("/missions")
async def create_mission(
    data: MissionCreate,
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
):
    wallet     = resolve_wallet(x_nwo_wallet, data.wallet)
    mission_id = generate_id("MIS")
    waypoints  = json.dumps(data.waypoints or [])

    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO missions (mission_id, wallet, robot_id, type, waypoints)
        VALUES (?, ?, ?, ?, ?)
    ''', (mission_id, wallet, data.robot_id, data.type, waypoints))
    db.commit()

    return {
        "status":     "success",
        "mission_id": mission_id,
        "type":       data.type,
        "message":    "Mission created",
    }


@app.get("/missions")
async def list_missions(
    db: sqlite3.Connection = Depends(get_db),
    x_nwo_wallet: Optional[str] = Header(None),
    wallet: Optional[str] = None,
    status: Optional[str] = None,
):
    resolved = resolve_wallet(x_nwo_wallet, wallet)

    cursor = db.cursor()
    if status:
        cursor.execute('''
            SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
            FROM missions WHERE wallet = ? AND status = ? ORDER BY created_at DESC
        ''', (resolved, status))
    else:
        cursor.execute('''
            SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
            FROM missions WHERE wallet = ? ORDER BY created_at DESC
        ''', (resolved,))

    missions = [
        {
            "id":           r["mission_id"],
            "robot_id":     r["robot_id"],
            "type":         r["type"],
            "status":       r["status"],
            "earnings_eth": r["earnings_eth"],
            "created_at":   r["created_at"],
            "completed_at": r["completed_at"],
        }
        for r in cursor.fetchall()
    ]
    return {"status": "success", "count": len(missions), "missions": missions}


# ── Health & root ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {
        "status":    "healthy",
        "service":   "nwo-robotics-api",
        "version":   "2.2.0",
        "db_path":   DB_PATH,
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/")
async def root():
    return {
        "service":     "NWO Robotics API",
        "version":     "2.2.0",
        "description": "FastAPI fallback for NWO Robotics ecosystem",
        "endpoints": {
            "api_keys":          "/api-keys  (GET, POST, DELETE /{key_id})",
            "api_keys_validate": "/api-keys/validate  (POST — Worker/agent sim key check)",
            "chat":              "/chat (POST), /chat/history (GET)",
            "model_usage":       "/model-usage (GET), /model-usage/track (POST)",
            "iot_networks":      "/iot-networks (GET, POST)",
            "robots":            "/robots (GET, POST)",
            "missions":          "/missions (GET, POST)",
            "health":            "/health",
        },
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
