# =============================================================================
# NWO Robotics API — Render Deployment (nwo-capital-api.onrender.com)
# main.py / app.py  ·  v3.0.0
# =============================================================================
# Complete rewrite. Changes from v2.2.0:
#   * STORAGE: SQLite (/data/nwo_api.db) -> Supabase Postgres
#       Set DATABASE_URL to the Supabase pooler connection string:
#       postgresql://postgres.gwnbrjibsirazwbhhenc:<PW>@aws-0-<region>.pooler.supabase.com:6543/postgres
#       Requires: nwo_capital_supabase_migration.sql + nwo_supabase_supplement.sql
#   * api_keys uses the reconciled wallet-scoped schema (key_hash, not api_key_hash)
#   * Tier-A PHP migrations are now NATIVE FastAPI routes wired to real tables
#     (agents, discovery, embodiment, calibration, RL, tactile, datasets, models,
#      safety, learning, finetune) — no more 501 stubs where a table exists.
#   * Layered microservices (Layers 2/3/4 + sim/forecast/regression/MR/etc.)
#     are reachable through thin REGISTRY/PROXY routes so the React UI keeps a
#     single base URL. Downstream URLs are env-configurable (SERVICES dict).
#   * Agent graph reads native from Supabase graph_nodes / graph_edges.
#   * /api/api-keys/validate contract preserved verbatim — nwo-simulation-api,
#     nwo-skill-engine and the Cloudflare runner all depend on it.
#
# AUTH MODEL (unchanged, lenient by default):
#   Wallet comes from X-NWO-Wallet header (set by the React apiFetch signer),
#   with a body/query `wallet` fallback for migration. When NWO_AUTH_REQUIRED=true
#   the signed-session headers (X-NWO-Wallet / X-NWO-Message / X-NWO-Signature)
#   are verified with eth_account and anonymous fallback is rejected.
# =============================================================================

import os
import json
import hashlib
import secrets
import base64
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

# psycopg2 connection pool (sync; FastAPI offloads sync `def` routes to a threadpool)
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
import psycopg2

# Optional signature verification (only used when NWO_AUTH_REQUIRED=true)
try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    _HAS_ETH = True
except Exception:  # eth_account not installed -> header-trust mode only
    _HAS_ETH = False


# =============================================================================
# Config
# =============================================================================
APP_VERSION   = "3.0.0"
DATABASE_URL  = os.getenv("DATABASE_URL", "")  # Supabase Postgres connection string
AUTH_REQUIRED = os.getenv("NWO_AUTH_REQUIRED", "false").lower() in ("1", "true", "yes")
PORT          = int(os.getenv("PORT", "8000"))

# v0.7.5 · Enactivist feedback channel to CHAINSTATE worker (Paper V §6.4)
# Set on Render: ENACTIVIST_BEARER = <same 32-byte hex used by CHAINSTATE worker>
CHAINSTATE_WORKER_URL = os.getenv(
    "CHAINSTATE_WORKER_URL",
    "https://chainstate-worker.ciprianpater.workers.dev"
)
ENACTIVIST_BEARER = os.getenv("ENACTIVIST_BEARER", "")

# Downstream layered services. Override any of these in the Render dashboard.
SERVICES: Dict[str, str] = {
    "parts_gallery":   os.getenv("SVC_PARTS_GALLERY",   "https://nwo-parts-gallery.onrender.com"),
    "skill_engine":    os.getenv("SVC_SKILL_ENGINE",    "https://nwo-skill-engine.onrender.com"),
    "printer":         os.getenv("SVC_PRINTER",         "https://nwo-printer-connectors.onrender.com"),
    "design_engine":   os.getenv("SVC_DESIGN_ENGINE",   "https://nwo-design-engine.onrender.com"),
    "text_cad":        os.getenv("SVC_TEXT_CAD",        "https://nwo-text-cad.onrender.com"),
    "market_layer":    os.getenv("SVC_MARKET_LAYER",    "https://nwo-market-layer.onrender.com"),
    "signal_spectrum": os.getenv("SVC_SIGNAL_SPECTRUM", "https://nwo-signal-spectrum.onrender.com"),
    "mr":              os.getenv("SVC_MR",              "https://nwo-mr.onrender.com"),
    "simulation":      os.getenv("SVC_SIMULATION",      "https://nwo-simulation-api.onrender.com"),
    "deerflow":        os.getenv("SVC_DEERFLOW",        "https://nwo-deerflow.onrender.com"),
    "timesfm":         os.getenv("SVC_TIMESFM",         "https://nwo-timesfm-integration.onrender.com"),
    "eml":             os.getenv("SVC_EML",             "https://nwo-eml-regression.onrender.com"),
    "agi":             os.getenv("SVC_AGI",             "https://nwo-agi.onrender.com"),
    "robotics_cs":     os.getenv("SVC_ROBOTICS_CS",     "https://nwo-robotics-cs.onrender.com"),
    "the_well":        os.getenv("SVC_THE_WELL",        "https://the-well-nwo-integration.onrender.com"),
    "langchain":       os.getenv("SVC_LANGCHAIN",       "https://langchain-nwo.onrender.com"),
    "ros2_bridge":     os.getenv("SVC_ROS2_BRIDGE",     "https://nwo-ros2-bridge.onrender.com"),
    "edge_inference":  os.getenv("SVC_EDGE_INFERENCE",  "https://nwo-robotics-api-edge.ciprianpater.workers.dev"),
    "oracle":          os.getenv("SVC_ORACLE",          "https://nwo-oracle.onrender.com"),
    "relayer":         os.getenv("SVC_RELAYER",         "https://nwo-relayer.onrender.com"),
}

# Base mainnet payment processor (Cardiac SDK) — used by /agents/{id}/pay
PAYMENT_PROCESSOR = os.getenv("PAYMENT_PROCESSOR", "0x4afa4618bb992a073dbcfbddd6d1aebc3d5abd7c")
TIER_PRICES_ETH   = {"free": 0.0, "prototype": 0.015, "production": 0.062}
TIER_QUOTAS       = {"free": 100_000, "prototype": 500_000, "production": None}  # None = unlimited

MODEL_COSTS = {"gpt-4": 0.0001, "claude-3": 0.00015, "llama-3": 0.00005, "timesfm": 0.00002}


# =============================================================================
# Database pool
# =============================================================================
_pool: Optional[ThreadedConnectionPool] = None


def init_pool():
    global _pool
    if not DATABASE_URL:
        print("⚠️  DATABASE_URL not set — DB endpoints will return 503.")
        return
    if _pool is None:
        _pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=int(os.getenv("DB_POOL_MAX", "8")),
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor,
        )
        print("✅ Supabase Postgres pool initialized.")


def db_query(sql: str, params: tuple = (), *, fetch: str = "all", commit: bool = False):
    """Run a query against Supabase. fetch in {'all','one','none'}."""
    if _pool is None:
        raise HTTPException(status_code=503, detail="database not configured (DATABASE_URL missing)")
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = None
            if fetch == "all":
                result = cur.fetchall()
            elif fetch == "one":
                result = cur.fetchone()
            if commit:
                conn.commit()
            return result
    except psycopg2.Error as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"db error: {e.pgerror or str(e)}")
    finally:
        _pool.putconn(conn)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8).upper()}"


def jsonable(rows):
    """Coerce RealDict rows -> plain json-serialisable (datetimes, Decimals)."""
    return json.loads(json.dumps(rows, default=str))


# =============================================================================
# FastAPI app
# =============================================================================
app = FastAPI(
    title="NWO Robotics API",
    description="Central wallet-scoped API + layered-service gateway for the NWO ecosystem.",
    version=APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nwo.capital",
        "https://cpater-nwo-capital.hf.space",
        "https://cpater-nwo-capital.static.hf.space",
        "https://nwo.ciprianpater.workers.dev",
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_origin_regex=r"https://.*\.(hf\.space|workers\.dev|onrender\.com)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=[
        "Content-Type", "Authorization",
        "X-NWO-Wallet", "X-NWO-Message", "X-NWO-Signature",
        "X-API-Key", "X-Agent-ID",
    ],
)


@app.on_event("startup")
def _startup():
    init_pool()


# =============================================================================
# Auth
# =============================================================================
def _verify_signature(wallet: str, b64_message: Optional[str], signature: Optional[str]) -> bool:
    """Recover signer from an eth personal_sign and compare to wallet."""
    if not (_HAS_ETH and b64_message and signature):
        return False
    try:
        message = base64.b64decode(b64_message).decode("utf-8")
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        return recovered.lower() == wallet.lower()
    except Exception:
        return False


def resolve_wallet(
    x_nwo_wallet: Optional[str],
    body_wallet: Optional[str] = None,
    *,
    x_nwo_message: Optional[str] = None,
    x_nwo_signature: Optional[str] = None,
) -> str:
    """
    Lenient (default): trust X-NWO-Wallet header, fall back to body/query wallet.
    Strict (NWO_AUTH_REQUIRED=true): require header wallet AND a valid signature.
    """
    wallet = (x_nwo_wallet or body_wallet or "").strip().lower()
    if not wallet:
        raise HTTPException(status_code=401, detail="wallet not provided — connect MetaMask")
    if AUTH_REQUIRED:
        if not x_nwo_wallet:
            raise HTTPException(status_code=401, detail="signed-session headers required")
        if not _verify_signature(wallet, x_nwo_message, x_nwo_signature):
            raise HTTPException(status_code=401, detail="invalid or missing session signature")
    return wallet


# =============================================================================
# Pydantic models
# =============================================================================
class ApiKeyCreate(BaseModel):
    name: str
    wallet: Optional[str] = None

class ApiKeyValidate(BaseModel):
    # Accept both the historic field names so existing callers keep working.
    api_key: Optional[str] = None
    key: Optional[str] = None
    wallet: Optional[str] = None

class ChatMessage(BaseModel):
    message: str
    robot_id: Optional[str] = None
    wallet: Optional[str] = None
    history: Optional[List[Dict]] = []

class ModelUsageTrack(BaseModel):
    model_id: str
    wallet: Optional[str] = None
    latency_ms: Optional[int] = None

class IoTNetworkCreate(BaseModel):
    name: str
    kind: Optional[str] = "wifi_csi"
    wallet: Optional[str] = None

class RobotCreate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = "mobile_manipulator"
    description: Optional[str] = None
    agent_address: Optional[str] = None
    embodiment_id: Optional[str] = None
    wallet: Optional[str] = None

class MissionCreate(BaseModel):
    goal: Optional[str] = None
    robot_id: Optional[str] = None
    type: Optional[str] = "general"
    waypoints: Optional[List[Dict]] = []
    wallet: Optional[str] = None

class AgentRegister(BaseModel):
    agent_name: Optional[str] = None
    name: Optional[str] = None
    capabilities: Optional[List[str]] = []
    wallet: Optional[str] = None

class AgentPay(BaseModel):
    tier: str
    tx_hash: Optional[str] = None
    wallet: Optional[str] = None

class CalibrationSave(BaseModel):
    robot_id: str
    calibration_type: Optional[str] = "vision_base"
    transform_se3: Optional[List[List[float]]] = None
    reprojection_err: Optional[float] = None
    notes: Optional[str] = None
    wallet: Optional[str] = None

class RlSessionCreate(BaseModel):
    robot_id: Optional[str] = None
    agent_id: Optional[str] = None
    policy_name: Optional[str] = None
    policy_version: Optional[str] = None
    config: Optional[Dict] = {}
    wallet: Optional[str] = None

class RlTelemetry(BaseModel):
    step: int
    episode: Optional[int] = None
    observation: Optional[Dict] = None
    action: Optional[Dict] = None
    reward: Optional[float] = None
    done: Optional[bool] = False
    info: Optional[Dict] = {}
    wallet: Optional[str] = None

class DatasetCreate(BaseModel):
    name: str
    format: Optional[str] = "unitree"
    storage_url: Optional[str] = None
    episode_count: Optional[int] = 0
    is_public: Optional[bool] = False
    wallet: Optional[str] = None

class LearningLog(BaseModel):
    instruction: str
    mission_id: Optional[str] = None
    robot_id: Optional[str] = None
    strategy: Optional[Dict] = {}
    outcome: Optional[str] = None
    duration_ms: Optional[int] = None
    metrics: Optional[Dict] = {}
    feedback: Optional[str] = None
    wallet: Optional[str] = None

class PrintJobCreate(BaseModel):
    part_id: Optional[str] = None
    printer_id: Optional[str] = None
    connector: Optional[str] = None
    gcode_url: Optional[str] = None
    material: Optional[str] = None
    wallet: Optional[str] = None

class MarketListingCreate(BaseModel):
    listing_type: Optional[str] = "part"
    ref_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    price_eth: Optional[float] = 0.0
    media: Optional[List] = []
    wallet: Optional[str] = None

class DesignCreate(BaseModel):
    name: str
    source: Optional[str] = "text_cad"
    prompt: Optional[str] = None
    format: Optional[str] = "stl"
    artifact_url: Optional[str] = None
    is_public: Optional[bool] = False
    wallet: Optional[str] = None


# =============================================================================
# Generic downstream proxy helper
# =============================================================================
async def proxy(service: str, path: str, method: str = "GET",
                params: dict = None, json_body: dict = None,
                headers: dict = None, timeout: float = 25.0) -> JSONResponse:
    base = SERVICES.get(service)
    if not base:
        raise HTTPException(status_code=502, detail=f"unknown service: {service}")
    url = base.rstrip("/") + "/" + path.lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, params=params, json=json_body, headers=headers or {})
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"{service} timed out (cold start? retry in ~30s)")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"{service} unreachable: {e}")
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct:
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    return Response(status_code=resp.status_code, content=resp.content, media_type=ct or "text/plain")


# =============================================================================
# PUBLIC ROUTES
# =============================================================================
@app.get("/")
def root():
    return {
        "service": "NWO Robotics API",
        "version": APP_VERSION,
        "storage": "supabase-postgres" if DATABASE_URL else "unconfigured",
        "auth": "session-signature (eth personal_sign)" if AUTH_REQUIRED else "wallet-header (lenient)",
        "groups": {
            "core":     ["/api/api-keys", "/api/chat", "/api/model-usage",
                         "/api/iot-networks", "/api/robots", "/api/missions"],
            "agents":   ["/api/agents", "/api/agents/{id}/balance", "/api/agents/{id}/pay"],
            "discovery":["/api/discovery/whoami", "/api/discovery/capabilities",
                         "/api/discovery/dry-run", "/api/discovery/plan", "/api/discovery/health"],
            "embodiment":["/api/embodiment", "/api/calibration"],
            "rl":       ["/api/rl/sessions"],
            "data":     ["/api/datasets", "/api/models", "/api/learning/log", "/api/tactile/orca"],
            "layers":   ["/api/parts", "/api/skills", "/api/print-jobs",
                         "/api/designs", "/api/market/listings", "/api/graph/nodes"],
            "proxy":    ["/api/forecast", "/api/regression", "/api/deerflow/run",
                         "/api/signal-spectrum", "/api/mr", "/api/sim"],
        },
    }


@app.get("/health")
@app.get("/api/health")
def health():
    db_ok = False
    if _pool is not None:
        try:
            db_query("SELECT 1", fetch="one")
            db_ok = True
        except Exception:
            db_ok = False
    return {
        "status": "healthy" if (db_ok or not DATABASE_URL) else "degraded",
        "service": "nwo-robotics-api",
        "version": APP_VERSION,
        "db": "ok" if db_ok else ("unconfigured" if not DATABASE_URL else "error"),
        "auth_enforced": AUTH_REQUIRED,
        "timestamp": now_iso(),
    }


@app.post("/api/auth/echo")
def auth_echo(
    x_nwo_wallet: Optional[str] = Header(None),
    x_nwo_message: Optional[str] = Header(None),
    x_nwo_signature: Optional[str] = Header(None),
):
    """Smoke test: confirms what the server resolved the caller's wallet to."""
    wallet = resolve_wallet(x_nwo_wallet, None,
                            x_nwo_message=x_nwo_message, x_nwo_signature=x_nwo_signature)
    return {"ok": True, "wallet": wallet, "verified": AUTH_REQUIRED}


# =============================================================================
# API KEYS  (reconciled wallet-scoped api_keys; uses key_hash)
# =============================================================================
@app.post("/api/api-keys")
def create_api_key(data: ApiKeyCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet  = resolve_wallet(x_nwo_wallet, data.wallet)
    key_id  = generate_id("KEY")
    api_key = "nwo_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    db_query(
        """INSERT INTO api_keys (id, wallet, name, key_hash, key_prefix, key_suffix, is_active)
           VALUES (%s, %s, %s, %s, %s, %s, true)""",
        (key_id, wallet, data.name, key_hash, api_key[:12], api_key[-4:]),
        fetch="none", commit=True,
    )
    return {
        "key_id": key_id, "id": key_id, "name": data.name,
        "api_key": api_key, "key": api_key,
        "key_prefix": api_key[:12],
        "key_preview": api_key[:12] + "…" + api_key[-4:],
        "created_at": now_iso(),
    }


@app.get("/api/api-keys")
def list_api_keys(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, name, key_prefix, key_suffix, usage_count, created_at, is_active, agent_id
           FROM api_keys WHERE wallet=%s AND is_active=true ORDER BY created_at DESC""",
        (resolved,),
    )
    keys = [{
        "id": r["id"], "key_id": r["id"], "name": r["name"],
        "key_prefix": r["key_prefix"], "key_suffix": r["key_suffix"],
        "key_preview": (r["key_prefix"] or "") + "…" + (r["key_suffix"] or ""),
        "usage_count": r["usage_count"], "created_at": str(r["created_at"]),
        "is_active": bool(r["is_active"]),
        # agent_id set => key was minted by/for an AI agent (automated system key)
        "agent_id": r["agent_id"], "is_agent": r["agent_id"] is not None,
    } for r in rows]
    return {"status": "success", "count": len(keys), "keys": keys}


@app.post("/api/api-keys/validate")
def validate_api_key(data: ApiKeyValidate):
    """Validate a raw key (called by nwo-simulation-api, skill engine, CF runner).
    Contract preserved: returns {valid, wallet, key_id, name, ...}. Never returns the hash."""
    raw = (data.api_key or data.key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="api_key (or key) is required")
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    row = db_query(
        """SELECT id, wallet, name, key_prefix, key_suffix, usage_count, is_active
           FROM api_keys WHERE key_hash=%s""",
        (key_hash,), fetch="one",
    )
    if not row:
        return {"valid": False, "error": "key not found"}
    if not row["is_active"]:
        return {"valid": False, "error": "key revoked"}
    if data.wallet and (row["wallet"] or "").lower() != data.wallet.strip().lower():
        raise HTTPException(status_code=403, detail="key does not belong to the provided wallet")
    db_query(
        "UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = now() WHERE id=%s",
        (row["id"],), fetch="none", commit=True,
    )
    return {
        "valid": True, "key_id": row["id"], "id": row["id"], "name": row["name"],
        "wallet": row["wallet"],
        "key_preview": (row["key_prefix"] or "") + "…" + (row["key_suffix"] or ""),
        "usage_count": (row["usage_count"] or 0) + 1,
        "is_test_key": False,
    }


@app.delete("/api/api-keys/{key_id}")
def revoke_api_key(key_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    row = db_query(
        "UPDATE api_keys SET is_active=false WHERE id=%s AND wallet=%s AND is_active=true RETURNING id",
        (key_id, resolved), fetch="one", commit=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="key not found or not owned by this wallet")
    return {"status": "success", "message": "key revoked", "key_id": key_id}


# =============================================================================
# CHAT
# =============================================================================
@app.post("/api/chat")
def chat(data: ChatMessage, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    db_query("INSERT INTO chat_messages (wallet, role, content) VALUES (%s,'user',%s)",
             (wallet, data.message), fetch="none", commit=True)
    canned = {
        "create":   "Go to Dashboard and click '+ Register Robot' to register a robot.",
        "fleet":    "Navigate to the Tracking tab to view your registered fleet.",
        "navigate": "Provide destination coordinates and I'll route the command.",
        "help":     "Available: create robot, view fleet, navigate, check status, earnings.",
        "earnings": "Earnings split: 35% Guardian / 35% Savings / 30% Operational.",
        "status":   "Connect your wallet and visit Tracking to see live robot status.",
    }
    reply = "I'm here to help with NWO Robotics. Ask about creating robots, viewing your fleet, or navigation."
    low = data.message.lower()
    for kw, r in canned.items():
        if kw in low:
            reply = r
            break
    if data.robot_id:
        reply = f"[robot:{data.robot_id}] " + reply
    db_query("INSERT INTO chat_messages (wallet, robot_id, role, content) VALUES (%s,%s,'assistant',%s)",
             (wallet, data.robot_id if data.robot_id else None, reply), fetch="none", commit=True)
    return {"status": "success", "response": reply, "reply": reply, "timestamp": now_iso()}


@app.get("/api/chat/history")
def chat_history(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None, limit: int = 50):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        "SELECT role, content, created_at FROM chat_messages WHERE wallet=%s ORDER BY created_at DESC LIMIT %s",
        (resolved, limit),
    )
    msgs = [{"role": r["role"], "content": r["content"], "timestamp": str(r["created_at"])} for r in rows]
    return {"status": "success", "count": len(msgs), "messages": list(reversed(msgs))}


# =============================================================================
# MODEL USAGE
# =============================================================================
@app.get("/api/model-usage")
def model_usage(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        "SELECT model_id, calls, cost_eth, avg_latency_ms, last_used_at FROM model_usage WHERE wallet=%s",
        (resolved,),
    )
    models, total_calls, total_cost = [], 0, 0.0
    for r in rows:
        models.append({"model_id": r["model_id"], "calls": r["calls"],
                       "cost_eth": float(r["cost_eth"] or 0),
                       "latency_ms": r["avg_latency_ms"], "last_used": str(r["last_used_at"])})
        total_calls += int(r["calls"] or 0)
        total_cost  += float(r["cost_eth"] or 0)
    return {"status": "success", "wallet": resolved,
            "usage": {"calls": total_calls, "cost": total_cost, "models": models}}


@app.post("/api/model-usage/track")
def track_model_usage(data: ModelUsageTrack, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    cost = MODEL_COSTS.get(data.model_id, 0.0001)
    db_query(
        """INSERT INTO model_usage (wallet, model_id, calls, cost_eth, avg_latency_ms, last_used_at)
           VALUES (%s,%s,1,%s,%s, now())
           ON CONFLICT (wallet, model_id) DO UPDATE SET
             calls = model_usage.calls + 1,
             cost_eth = model_usage.cost_eth + EXCLUDED.cost_eth,
             avg_latency_ms = COALESCE(EXCLUDED.avg_latency_ms, model_usage.avg_latency_ms),
             last_used_at = now()""",
        (wallet, data.model_id, cost, data.latency_ms), fetch="none", commit=True,
    )
    return {"status": "success", "message": "usage tracked", "cost_eth": cost}


# =============================================================================
# IOT NETWORKS
# =============================================================================
@app.post("/api/iot-networks")
def create_iot_network(data: IoTNetworkCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        "INSERT INTO iot_networks (wallet, name, kind) VALUES (%s,%s,%s) RETURNING id",
        (wallet, data.name, data.kind or "wifi_csi"), fetch="one", commit=True,
    )
    return {"status": "success", "network_id": str(row["id"]), "id": str(row["id"]), "name": data.name}


@app.get("/api/iot-networks")
def list_iot_networks(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, name, kind, status, device_count, data_points, created_at
           FROM iot_networks WHERE wallet=%s ORDER BY created_at DESC""", (resolved,),
    )
    nets = [{"id": str(r["id"]), "name": r["name"], "kind": r["kind"], "status": r["status"],
             "device_count": r["device_count"], "data_points": r["data_points"],
             "created_at": str(r["created_at"])} for r in rows]
    return {"status": "success", "count": len(nets), "networks": nets}


# =============================================================================
# ROBOTS
# =============================================================================
@app.post("/api/robots")
def create_robot(data: RobotCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    name = data.name or f"Robot {secrets.token_hex(2).upper()}"
    row = db_query(
        """INSERT INTO robots (wallet, name, robot_type, description, embodiment_id, metadata)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, name, data.type or "mobile_manipulator", data.description,
         data.embodiment_id, json.dumps({"agent_address": data.agent_address} if data.agent_address else {})),
        fetch="one", commit=True,
    )
    rid = str(row["id"])
    return {"status": "success", "id": rid, "robot_id": rid, "name": name, "message": "Robot registered"}


@app.get("/api/robots")
def list_robots(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, name, robot_type, description, status, embodiment_id, metadata, created_at, last_seen_at
           FROM robots WHERE wallet=%s ORDER BY created_at DESC""", (resolved,),
    )
    robots = [{"id": str(r["id"]), "robot_id": str(r["id"]), "name": r["name"],
               "type": r["robot_type"], "description": r["description"], "status": r["status"],
               "embodiment_id": str(r["embodiment_id"]) if r["embodiment_id"] else None,
               "agent_address": (r["metadata"] or {}).get("agent_address"),
               "created_at": str(r["created_at"]), "last_seen": str(r["last_seen_at"])} for r in rows]
    return {"status": "success", "count": len(robots), "robots": robots}


@app.get("/api/robots/{robot_id}")
def get_robot(robot_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    r = db_query("SELECT * FROM robots WHERE id=%s AND wallet=%s", (robot_id, resolved), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail="robot not found")
    return jsonable(r)


# =============================================================================
# MISSIONS
# =============================================================================
@app.post("/api/missions")
def create_mission(data: MissionCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    goal = (data.goal or "").strip() or f"{data.type} mission"
    row = db_query(
        """INSERT INTO missions (wallet, robot_id, goal, mission_type, waypoints)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.robot_id if data.robot_id else None, goal,
         data.type or "general", json.dumps(data.waypoints or [])),
        fetch="one", commit=True,
    )
    mid = str(row["id"])
    return {"status": "success", "mission_id": mid, "id": mid, "type": data.type, "message": "Mission created"}


@app.get("/api/missions")
def list_missions(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None,
                  status: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    if status:
        rows = db_query(
            """SELECT id, robot_id, goal, mission_type, status, earnings_eth, created_at, completed_at
               FROM missions WHERE wallet=%s AND status=%s ORDER BY created_at DESC""",
            (resolved, status))
    else:
        rows = db_query(
            """SELECT id, robot_id, goal, mission_type, status, earnings_eth, created_at, completed_at
               FROM missions WHERE wallet=%s ORDER BY created_at DESC""", (resolved,))
    missions = [{"id": str(r["id"]), "robot_id": str(r["robot_id"]) if r["robot_id"] else None,
                 "goal": r["goal"], "type": r["mission_type"], "status": r["status"],
                 "earnings_eth": float(r["earnings_eth"] or 0),
                 "created_at": str(r["created_at"]), "completed_at": str(r["completed_at"])} for r in rows]
    return {"status": "success", "count": len(missions), "missions": missions}


@app.get("/api/missions/{mission_id}")
def get_mission(mission_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    r = db_query("SELECT * FROM missions WHERE id=%s AND wallet=%s", (mission_id, resolved), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail="mission not found")
    return jsonable(r)


# =============================================================================
# AGENT MANAGEMENT  (Group 4) — backed by agent_dids + identities + token_*
# =============================================================================
def _wallet_to_agents(wallet: str) -> List[dict]:
    """Resolve a wallet -> its agents via identities.primary_wallet -> agent_dids.did."""
    return db_query(
        """SELECT a.id, a.did, a.name, a.is_robot, a.robot_type, a.is_active, a.last_seen_at
           FROM identities i JOIN agent_dids a ON a.did = i.nwo_did
           WHERE lower(i.primary_wallet) = %s""", (wallet,)
    ) or []


@app.post("/api/agents")
def register_agent(data: AgentRegister, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    name = (data.agent_name or data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="agent_name required")
    did = f"did:nwo:base:{secrets.token_hex(8)}"
    agent_id = generate_id("AGENT")
    # Create the agent identity. is_robot defaults false; capabilities go to agent_capabilities.
    db_query(
        """INSERT INTO agent_dids (id, did, name, is_active, is_robot)
           VALUES (%s,%s,%s,true,false)""",
        (agent_id, did, name), fetch="none", commit=True,
    )
    # Open a token account so balance/pay work immediately.
    db_query(
        """INSERT INTO token_accounts (id, agent_id, balance, total_earned, total_spent)
           VALUES (%s,%s,0,0,0) ON CONFLICT DO NOTHING""",
        (generate_id("ACC"), agent_id), fetch="none", commit=True,
    )
    for cap in (data.capabilities or []):
        db_query(
            """INSERT INTO agent_capabilities (agent_id, capability)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (agent_id, cap), fetch="none", commit=True,
        )
    # Mint a wallet-scoped API key for the agent.
    api_key = "nwo_" + secrets.token_urlsafe(32)
    key_id = generate_id("KEY")
    db_query(
        """INSERT INTO api_keys (id, wallet, agent_id, name, key_hash, key_prefix, key_suffix, is_active)
           VALUES (%s,%s,%s,%s,%s,%s,%s,true)""",
        (key_id, wallet, agent_id, f"{name} key",
         hashlib.sha256(api_key.encode()).hexdigest(), api_key[:12], api_key[-4:]),
        fetch="none", commit=True,
    )
    return {"agent_id": agent_id, "did": did, "name": name, "tier": "free",
            "api_key": api_key, "key": api_key}


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolve_wallet(x_nwo_wallet, wallet)
    r = db_query("SELECT * FROM agent_dids WHERE id=%s", (agent_id,), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail="agent not found")
    return jsonable(r)


@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    fields, vals = [], []
    for k in ("name", "robot_type", "is_active", "description"):
        if k in body:
            fields.append(f"{k}=%s")
            vals.append(body[k])
    if not fields:
        return {"status": "noop"}
    vals.append(agent_id)
    db_query(f"UPDATE agent_dids SET {', '.join(fields)} WHERE id=%s", tuple(vals),
             fetch="none", commit=True)
    return {"status": "success", "agent_id": agent_id}


@app.get("/api/agents/{agent_id}/balance")
def agent_balance(agent_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolve_wallet(x_nwo_wallet, wallet)
    acc = db_query("SELECT balance, total_earned, total_spent FROM token_accounts WHERE agent_id=%s",
                   (agent_id,), fetch="one")
    usage = db_query(
        """SELECT COALESCE(SUM(mu.calls),0) AS calls
           FROM api_keys k JOIN model_usage mu ON mu.wallet = k.wallet
           WHERE k.agent_id = %s""", (agent_id,), fetch="one")
    # Tier is inferred from token_ledger tier_upgrade entries (latest wins) if present.
    tier_row = db_query(
        """SELECT to_tier FROM agent_tier_changes WHERE agent_id=%s ORDER BY created_at DESC LIMIT 1""",
        (agent_id,), fetch="one")
    tier = tier_row["to_tier"] if tier_row else "free"
    limit = TIER_QUOTAS.get(tier)
    calls_used = int(usage["calls"]) if usage else 0
    return {
        "agent_id": agent_id, "tier": tier,
        "balance": float(acc["balance"]) if acc else 0,
        "total_earned": float(acc["total_earned"]) if acc else 0,
        "total_spent": float(acc["total_spent"]) if acc else 0,
        "quota_limit": limit, "calls_used": calls_used,
        "quota_remaining": None if limit is None else max(0, limit - calls_used),
    }


@app.post("/api/agents/{agent_id}/pay")
def agent_pay(agent_id: str, data: AgentPay, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    if data.tier not in TIER_PRICES_ETH:
        raise HTTPException(status_code=400, detail="invalid tier")
    if data.tier != "free" and not data.tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required for paid tiers")
    cost = TIER_PRICES_ETH[data.tier]
    # NOTE: on-chain verification of tx_hash against PAYMENT_PROCESSOR is delegated
    # to the relayer service; here we record the tier change + ledger entry.
    prev = db_query(
        "SELECT to_tier FROM agent_tier_changes WHERE agent_id=%s ORDER BY created_at DESC LIMIT 1",
        (agent_id,), fetch="one")
    from_tier = prev["to_tier"] if prev else "free"
    ledger_id = generate_id("LDG")
    acc = db_query("SELECT id, balance FROM token_accounts WHERE agent_id=%s", (agent_id,), fetch="one")
    if acc:
        db_query(
            """INSERT INTO token_ledger (id, account_id, amount, reason, reference_id, balance_after)
               VALUES (%s,%s,%s,'tier_upgrade',%s,%s)""",
            (ledger_id, acc["id"], 0, data.tx_hash or "", acc["balance"]),
            fetch="none", commit=True)
    db_query(
        """INSERT INTO agent_tier_changes (agent_id, wallet, from_tier, to_tier, cost_eth, tx_hash, token_ledger_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (agent_id, wallet, from_tier, data.tier, cost, data.tx_hash, ledger_id if acc else None),
        fetch="none", commit=True)
    return {"agent_id": agent_id, "tier": data.tier, "from_tier": from_tier,
            "cost_eth": cost, "tx_hash": data.tx_hash, "status": "upgraded"}


# =============================================================================
# AGENT DISCOVERY  (Group 5)
# =============================================================================
@app.get("/api/discovery/health")
def discovery_health():
    return {"service": "NWO Robotics Discovery", "status": "ok",
            "version": APP_VERSION, "time": int(datetime.now(timezone.utc).timestamp())}


@app.get("/api/discovery/whoami")
def discovery_whoami(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    agents = _wallet_to_agents(resolved)
    return {"wallet": resolved,
            "agents": [{"id": a["id"], "did": a["did"], "name": a["name"],
                        "is_robot": a["is_robot"], "robot_type": a["robot_type"],
                        "status": "active" if a["is_active"] else "inactive"} for a in agents]}


@app.get("/api/discovery/capabilities")
def discovery_capabilities(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    tier_row = db_query(
        """SELECT to_tier FROM agent_tier_changes WHERE wallet=%s ORDER BY created_at DESC LIMIT 1""",
        (resolved,), fetch="one")
    tier = tier_row["to_tier"] if tier_row else "free"
    return {"tier": tier, "capabilities": {
        "inference":    {"available": True, "host": SERVICES["edge_inference"]},
        "robots":       {"available": True, "host": "nwo-capital-api.onrender.com"},
        "iot_networks": {"available": True, "host": "nwo-capital-api.onrender.com"},
        "missions":     {"available": True, "host": "nwo-capital-api.onrender.com"},
        "skills":       {"available": True, "host": SERVICES["skill_engine"]},
        "parts":        {"available": True, "host": SERVICES["parts_gallery"]},
        "print":        {"available": True, "host": SERVICES["printer"]},
        "simulation":   {"available": tier != "free", "host": SERVICES["simulation"], "note": "paid tier"},
        "ros2_bridge":  {"available": tier != "free", "host": SERVICES["ros2_bridge"], "note": "paid tier"},
    }}


@app.post("/api/discovery/dry-run")
def discovery_dry_run(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    action = body.get("action", "inference")
    table = {
        "inference":      {"valid": True, "estimated_cost_eth": 0.00002, "estimated_latency_ms": 142,
                           "safety_checks": ["within_force_limit", "within_speed_limit"]},
        "mission_deploy": {"valid": True, "estimated_cost_eth": 0.0001, "estimated_latency_ms": 50,
                           "safety_checks": ["mission_goal_valid", "agent_authorized"]},
        "print_job":      {"valid": True, "estimated_cost_eth": 0.0003, "estimated_latency_ms": 200,
                           "safety_checks": ["printer_online", "material_available"]},
    }
    if action not in table:
        return JSONResponse(status_code=400, content={"valid": False, "error": f"unknown action: {action}"})
    return table[action]


@app.post("/api/discovery/plan")
def discovery_plan(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    intent = body.get("intent", "")
    return {"intent": intent, "steps": [
        {"order": 1, "kind": "inspect",   "endpoint": "/api/robots"},
        {"order": 2, "kind": "inference", "endpoint": SERVICES["edge_inference"] + "/api/inference"},
        {"order": 3, "kind": "monitor",   "endpoint": "/api/missions"},
    ], "note": "skeleton plan — full LLM planner runs on GPU host"}


# =============================================================================
# EMBODIMENT & CALIBRATION  (Group 9) — embodiments native from robot_embodiments
# =============================================================================
@app.get("/api/embodiment")
def embodiment_list():
    rows = db_query(
        "SELECT embodiment_key, name, manufacturer, robot_class, joint_count FROM robot_embodiments ORDER BY name")
    return {"count": len(rows),
            "robots": [{"id": r["embodiment_key"], "name": r["name"], "manufacturer": r["manufacturer"],
                        "class": r["robot_class"], "dof": r["joint_count"]} for r in rows]}


@app.get("/api/embodiment/{key}")
def embodiment_detail(key: str):
    r = db_query("SELECT * FROM robot_embodiments WHERE embodiment_key=%s", (key,), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail=f"unknown embodiment: {key}")
    return jsonable(r)


@app.get("/api/embodiment/{key}/normalization")
def embodiment_normalization(key: str):
    r = db_query("SELECT action_normalization FROM robot_embodiments WHERE embodiment_key=%s",
                 (key,), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail=f"unknown embodiment: {key}")
    return {"id": key, "normalization": r["action_normalization"]}


@app.get("/api/embodiment/{key}/urdf")
def embodiment_urdf(key: str):
    r = db_query("SELECT urdf_url, urdf_sha256 FROM robot_embodiments WHERE embodiment_key=%s",
                 (key,), fetch="one")
    if not r or not r["urdf_url"]:
        raise HTTPException(status_code=404, detail=f"no URDF for {key}")
    return {"id": key, "urdf_url": r["urdf_url"], "urdf_sha256": r["urdf_sha256"]}


@app.post("/api/embodiment/compare")
def embodiment_compare(body: Dict[str, Any]):
    ids = body.get("robot_types") or body.get("ids") or []
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(status_code=400, detail="robot_types must be a list of 2+ ids")
    rows = db_query("SELECT * FROM robot_embodiments WHERE embodiment_key = ANY(%s)", (ids,))
    found = {r["embodiment_key"] for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status_code=404, detail=f"unknown: {missing}")
    return {"comparison": jsonable(rows)}


@app.post("/api/calibration")
def calibration_save(data: CalibrationSave, x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, data.wallet)
    if not data.transform_se3:
        raise HTTPException(status_code=400, detail="transform_se3 required")
    row = db_query(
        """INSERT INTO robot_calibrations (robot_id, calibration_type, transform_se3, reprojection_err, metadata)
           VALUES (%s,%s,%s,%s,%s) RETURNING id, created_at""",
        (data.robot_id, data.calibration_type, json.dumps(data.transform_se3),
         data.reprojection_err, json.dumps({"notes": data.notes} if data.notes else {})),
        fetch="one", commit=True)
    return {"id": str(row["id"]), "created_at": str(row["created_at"]), "status": "saved"}


@app.get("/api/calibration")
def calibration_list(robot_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, calibration_type, transform_se3, reprojection_err, created_at, is_active
           FROM robot_calibrations WHERE robot_id=%s AND is_active=true ORDER BY created_at DESC""",
        (robot_id,))
    return {"robot_id": robot_id, "calibrations": jsonable(rows)}


@app.post("/api/calibration/run")
async def calibration_run(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    robot_id = body.get("robot_id")
    if not robot_id:
        raise HTTPException(status_code=400, detail="robot_id required")
    return await proxy("ros2_bridge", "/api/v1/action", "POST",
                       json_body={"robot_id": robot_id, "action": "calibrate",
                                  "params": body.get("params") or {}})


# =============================================================================
# ONLINE RL  (Group 10)
# =============================================================================
@app.post("/api/rl/sessions")
def rl_start(data: RlSessionCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO rl_sessions (wallet, robot_id, agent_id, policy_name, policy_version, hyperparameters)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, status, started_at""",
        (wallet, data.robot_id if data.robot_id else None, data.agent_id,
         data.policy_name, data.policy_version, json.dumps(data.config or {})),
        fetch="one", commit=True)
    return {"session_id": str(row["id"]), "status": row["status"], "started_at": str(row["started_at"])}


@app.get("/api/rl/sessions")
def rl_list(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, robot_id, policy_name, status, total_steps, total_reward, started_at, ended_at
           FROM rl_sessions WHERE wallet=%s ORDER BY started_at DESC""", (resolved,))
    return {"count": len(rows), "sessions": jsonable(rows)}


@app.post("/api/rl/sessions/{session_id}/telemetry")
def rl_telemetry(session_id: str, data: RlTelemetry, x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, data.wallet)
    db_query(
        """INSERT INTO rl_telemetry (session_id, step, episode, observation, action, reward, done, info)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (session_id, step) DO NOTHING""",
        (session_id, data.step, data.episode,
         json.dumps(data.observation) if data.observation else None,
         json.dumps(data.action) if data.action else None,
         data.reward, data.done, json.dumps(data.info or {})),
        fetch="none", commit=True)
    db_query(
        """UPDATE rl_sessions SET total_steps = GREATEST(total_steps, %s),
             total_reward = total_reward + COALESCE(%s,0) WHERE id=%s""",
        (data.step, data.reward, session_id), fetch="none", commit=True)
    return {"recorded": True, "session_id": session_id, "step": data.step}


# =============================================================================
# TACTILE  (Group 11 — ORCA hand)
# =============================================================================
@app.get("/api/tactile/orca")
def tactile_orca(robot_id: str, x_nwo_wallet: Optional[str] = Header(None),
                 wallet: Optional[str] = None, limit: int = 100):
    resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT taxel_index, pressure_kpa, shear_x, shear_y, temperature_c, slip_detected, captured_at
           FROM tactile_streams WHERE robot_id=%s ORDER BY captured_at DESC LIMIT %s""",
        (robot_id, limit))
    if not rows:
        raise HTTPException(status_code=404, detail="no tactile readings for this robot")
    return {"robot_id": robot_id, "count": len(rows), "taxels": jsonable(rows)}


@app.post("/api/tactile")
def tactile_ingest(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    robot_id = body.get("robot_id")
    samples = body.get("samples") or [body]
    if not robot_id:
        raise HTTPException(status_code=400, detail="robot_id required")
    for s in samples:
        db_query(
            """INSERT INTO tactile_streams
                 (robot_id, taxel_index, pressure_kpa, shear_x, shear_y, temperature_c, slip_detected, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (robot_id, s.get("taxel_index"), s.get("pressure_kpa"), s.get("shear_x"),
             s.get("shear_y"), s.get("temperature_c"), bool(s.get("slip_detected", False)),
             json.dumps(s.get("raw")) if s.get("raw") else None),
            fetch="none", commit=True)
    return {"ingested": len(samples), "robot_id": robot_id}


# =============================================================================
# DATASETS  (Group 12)
# =============================================================================
@app.get("/api/datasets")
def datasets_list(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None,
                  public: bool = False):
    if public:
        rows = db_query(
            "SELECT id, name, format, episode_count, size_bytes, storage_url, created_at "
            "FROM datasets WHERE is_public=true ORDER BY created_at DESC")
    else:
        resolved = resolve_wallet(x_nwo_wallet, wallet)
        rows = db_query(
            "SELECT id, name, format, episode_count, size_bytes, storage_url, is_public, created_at "
            "FROM datasets WHERE wallet=%s ORDER BY created_at DESC", (resolved,))
    return {"count": len(rows), "datasets": jsonable(rows)}


@app.post("/api/datasets")
def dataset_create(data: DatasetCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO datasets (wallet, name, format, episode_count, storage_url, is_public)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.name, data.format, data.episode_count, data.storage_url, data.is_public),
        fetch="one", commit=True)
    return {"id": str(row["id"]), "name": data.name, "status": "registered"}


# =============================================================================
# MODELS  (Group 1 registry — static catalog; inference runs on GPU host)
# =============================================================================
MODEL_REGISTRY = [
    {"id": "nwo-vla-base-v2", "name": "NWO VLA Base v2", "kind": "vla",
     "host": SERVICES["edge_inference"], "latency_ms": 142, "cost_eth": 0.0001,
     "capabilities": ["pick_place", "navigate", "sort"]},
    {"id": "nwo-vla-fast-v1", "name": "NWO VLA Fast v1", "kind": "vla",
     "host": SERVICES["edge_inference"], "latency_ms": 89, "cost_eth": 0.00005,
     "capabilities": ["pick_place", "navigate"]},
    {"id": "nwo-vla-precision-v1", "name": "NWO VLA Precision v1", "kind": "vla",
     "host": "nwo.capital", "latency_ms": 312, "cost_eth": 0.00015,
     "capabilities": ["pick_place", "sort", "fine_assembly", "tactile_grasp"]},
    {"id": "xiaomi-robotics-0", "name": "Xiaomi Robotics v0", "kind": "vla",
     "host": "nwo.capital", "latency_ms": 220, "cost_eth": 0.0001,
     "capabilities": ["pick_place", "navigate", "sort"]},
]


@app.get("/api/models")
def models_list():
    return {"count": len(MODEL_REGISTRY), "models": MODEL_REGISTRY}


@app.get("/api/models/{model_id}")
def model_info(model_id: str):
    m = next((x for x in MODEL_REGISTRY if x["id"] == model_id), None)
    if not m:
        raise HTTPException(status_code=404, detail=f"unknown model_id: {model_id}")
    return m


# =============================================================================
# LEARNING  (Task planning & learning, Group 3)
# =============================================================================
@app.post("/api/learning/log")
def learning_log(data: LearningLog, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO task_executions
             (wallet, mission_id, robot_id, instruction, strategy, outcome, duration_ms, metrics, feedback)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.mission_id if data.mission_id else None,
         data.robot_id if data.robot_id else None, data.instruction,
         json.dumps(data.strategy or {}), data.outcome, data.duration_ms,
         json.dumps(data.metrics or {}), data.feedback),
        fetch="one", commit=True)
    return {"id": str(row["id"]), "status": "logged"}


@app.get("/api/learning/history")
def learning_history(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None, limit: int = 50):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, instruction, outcome, duration_ms, metrics, created_at
           FROM task_executions WHERE wallet=%s ORDER BY created_at DESC LIMIT %s""",
        (resolved, limit))
    return {"count": len(rows), "executions": jsonable(rows)}


@app.get("/api/learning/recommend")
def learning_recommend(instruction: str, embodiment: Optional[str] = None):
    ih = hashlib.sha256(instruction.lower().strip().encode()).hexdigest()
    row = db_query(
        "SELECT strategy, success_rate, sample_size FROM task_recommendations WHERE instruction_hash=%s",
        (ih,), fetch="one")
    if not row:
        return {"instruction": instruction, "strategy": None,
                "note": "no cached recommendation — falls back to GPU planner"}
    return {"instruction": instruction, **jsonable(row)}


# =============================================================================
# SAFETY
# =============================================================================
@app.post("/api/safety/violation")
def safety_violation(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, body.get("wallet"))
    db_query(
        """INSERT INTO safety_violations
             (wallet, robot_id, mission_id, violation_type, observed_value, limit_value,
              emergency_stop_triggered, metadata)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (wallet, body.get("robot_id"), body.get("mission_id"),
         body.get("violation_type", "other"), body.get("observed_value"),
         body.get("limit_value"), bool(body.get("emergency_stop_triggered", False)),
         json.dumps(body.get("metadata") or {})),
        fetch="none", commit=True)
    return {"recorded": True}


@app.get("/api/safety/violations")
def safety_list(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, robot_id, violation_type, observed_value, limit_value, resolved,
                  emergency_stop_triggered, created_at
           FROM safety_violations WHERE wallet=%s ORDER BY created_at DESC LIMIT 100""", (resolved,))
    return {"count": len(rows), "violations": jsonable(rows)}


# =============================================================================
# FINETUNE  (job state machine; GPU training body runs on nwo.capital)
# =============================================================================
@app.post("/api/finetune")
def finetune_create(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, body.get("wallet"))
    if not body.get("base_model"):
        raise HTTPException(status_code=400, detail="base_model required")
    row = db_query(
        """INSERT INTO finetune_jobs (wallet, dataset_id, base_model, job_type, hyperparameters)
           VALUES (%s,%s,%s,%s,%s) RETURNING id, status""",
        (wallet, body.get("dataset_id"), body["base_model"], body.get("job_type", "lora"),
         json.dumps(body.get("hyperparameters") or {})), fetch="one", commit=True)
    return {"job_id": str(row["id"]), "status": row["status"]}


@app.get("/api/finetune/{job_id}")
def finetune_status(job_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    r = db_query("SELECT * FROM finetune_jobs WHERE id=%s AND wallet=%s", (job_id, resolved), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    return jsonable(r)


# =============================================================================
# AGENT GRAPH  (native — Supabase graph_nodes / graph_edges)
# =============================================================================
@app.get("/api/graph/nodes")
def graph_nodes(agent_id: Optional[str] = None, node_type: Optional[str] = None,
                public_only: bool = True, limit: int = 200):
    clauses, params = [], []
    if public_only:
        clauses.append("is_public = true")
    if agent_id:
        clauses.append("agent_id = %s"); params.append(agent_id)
    if node_type:
        clauses.append("node_type = %s"); params.append(node_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = db_query(
        f"""SELECT id, agent_id, node_type, title, tags, is_public,
                   layer2_part_id, layer4_skill_id, created_at
            FROM graph_nodes {where} ORDER BY created_at DESC LIMIT %s""", tuple(params))
    return {"count": len(rows), "nodes": jsonable(rows)}


@app.get("/api/graph/edges")
def graph_edges(node_id: Optional[str] = None, limit: int = 500):
    if node_id:
        rows = db_query(
            """SELECT id, source_node_id, target_node_id, relation, weight, created_at
               FROM graph_edges WHERE source_node_id=%s OR target_node_id=%s
               ORDER BY created_at DESC LIMIT %s""", (node_id, node_id, limit))
    else:
        rows = db_query(
            """SELECT id, source_node_id, target_node_id, relation, weight, created_at
               FROM graph_edges ORDER BY created_at DESC LIMIT %s""", (limit,))
    return {"count": len(rows), "edges": jsonable(rows)}


# =============================================================================
# LAYER 3 — PRINT JOBS  (native ledger; execution proxied to printer service)
# =============================================================================
@app.post("/api/print-jobs")
async def print_job_create(data: PrintJobCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO print_jobs (wallet, part_id, printer_id, connector, gcode_url, material)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.part_id, data.printer_id, data.connector, data.gcode_url, data.material),
        fetch="one", commit=True)
    job_id = str(row["id"])
    # Best-effort dispatch to the printer connector service (non-fatal if it's cold).
    try:
        await proxy("printer", "/jobs", "POST",
                    json_body={"job_id": job_id, "gcode_url": data.gcode_url,
                               "printer_id": data.printer_id, "connector": data.connector})
    except HTTPException:
        pass
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/print-jobs")
def print_jobs_list(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, part_id, printer_id, connector, status, progress, material, created_at
           FROM print_jobs WHERE wallet=%s ORDER BY created_at DESC""", (resolved,))
    return {"count": len(rows), "jobs": jsonable(rows)}


@app.get("/api/print-jobs/{job_id}")
def print_job_get(job_id: str, x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    r = db_query("SELECT * FROM print_jobs WHERE id=%s AND wallet=%s", (job_id, resolved), fetch="one")
    if not r:
        raise HTTPException(status_code=404, detail="print job not found")
    return jsonable(r)


# =============================================================================
# MARKET LAYER — listings (native)
# =============================================================================
@app.get("/api/market/listings")
def market_list(listing_type: Optional[str] = None, limit: int = 100):
    if listing_type:
        rows = db_query(
            """SELECT id, listing_type, ref_id, title, description, price_eth, media, created_at
               FROM marketplace_listings WHERE status='active' AND listing_type=%s
               ORDER BY created_at DESC LIMIT %s""", (listing_type, limit))
    else:
        rows = db_query(
            """SELECT id, listing_type, ref_id, title, description, price_eth, media, created_at
               FROM marketplace_listings WHERE status='active' ORDER BY created_at DESC LIMIT %s""", (limit,))
    return {"count": len(rows), "listings": jsonable(rows)}


@app.post("/api/market/listings")
def market_create(data: MarketListingCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO marketplace_listings (wallet, listing_type, ref_id, title, description, price_eth, media)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.listing_type, data.ref_id, data.title, data.description,
         data.price_eth, json.dumps(data.media or [])), fetch="one", commit=True)
    return {"id": str(row["id"]), "status": "listed"}


# =============================================================================
# DESIGNS — text-cad / design-engine artifacts (native ledger + proxy generate)
# =============================================================================
@app.get("/api/designs")
def designs_list(x_nwo_wallet: Optional[str] = Header(None), wallet: Optional[str] = None):
    resolved = resolve_wallet(x_nwo_wallet, wallet)
    rows = db_query(
        """SELECT id, name, source, format, artifact_url, thumbnail_url, is_public, created_at
           FROM designs WHERE wallet=%s ORDER BY created_at DESC""", (resolved,))
    return {"count": len(rows), "designs": jsonable(rows)}


@app.post("/api/designs")
def design_create(data: DesignCreate, x_nwo_wallet: Optional[str] = Header(None)):
    wallet = resolve_wallet(x_nwo_wallet, data.wallet)
    row = db_query(
        """INSERT INTO designs (wallet, name, source, prompt, format, artifact_url, is_public)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (wallet, data.name, data.source, data.prompt, data.format, data.artifact_url, data.is_public),
        fetch="one", commit=True)
    return {"id": str(row["id"]), "status": "saved"}


@app.post("/api/text-cad/generate")
async def text_cad_generate(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("text_cad", "/generate", "POST", json_body=body)


# =============================================================================
# LAYER 2/4 PROXIES — parts gallery & skill engine (single base URL for UI)
# =============================================================================
@app.get("/api/parts")
async def parts_list(q: Optional[str] = None, limit: int = 50):
    return await proxy("parts_gallery", "/parts", "GET",
                       params={k: v for k, v in {"q": q, "limit": limit}.items() if v is not None})

@app.get("/api/parts/{part_id}")
async def parts_get(part_id: str):
    return await proxy("parts_gallery", f"/parts/{part_id}", "GET")

@app.get("/api/skills")
async def skills_search(q: Optional[str] = None, skill_type: Optional[str] = None):
    return await proxy("skill_engine", "/skills/search", "GET",
                       params={k: v for k, v in {"q": q, "skill_type": skill_type}.items() if v is not None})

@app.get("/api/skills/{skill_id}")
async def skills_get(skill_id: str):
    return await proxy("skill_engine", f"/skills/{skill_id}", "GET")

@app.post("/api/skills/{skill_id}/run")
async def skills_run(skill_id: str, body: Dict[str, Any], x_agent_id: Optional[str] = Header(None)):
    return await proxy("skill_engine", f"/skills/{skill_id}/run", "POST",
                       json_body=body, headers={"X-Agent-ID": x_agent_id} if x_agent_id else None)

@app.get("/api/agents/{agent_id}/skills")
async def agent_skills(agent_id: str):
    return await proxy("skill_engine", f"/agents/{agent_id}/skills", "GET")


# =============================================================================
# COMPUTE PROXIES — sim / forecast / regression / deerflow / signal / mr / etc.
# =============================================================================
@app.post("/api/sim/environments")
async def sim_env(body: Dict[str, Any], x_api_key: Optional[str] = Header(None)):
    return await proxy("simulation", "/v1/environments", "POST", json_body=body,
                       headers={"X-API-Key": x_api_key} if x_api_key else None)

@app.post("/api/sim/simulations")
async def sim_run(body: Dict[str, Any], x_api_key: Optional[str] = Header(None)):
    return await proxy("simulation", "/v1/simulations", "POST", json_body=body,
                       headers={"X-API-Key": x_api_key} if x_api_key else None)

@app.get("/api/sim/simulations/{sim_id}/results")
async def sim_results(sim_id: str, x_api_key: Optional[str] = Header(None)):
    return await proxy("simulation", f"/v1/simulations/{sim_id}/results", "GET",
                       headers={"X-API-Key": x_api_key} if x_api_key else None)

@app.post("/api/forecast")
async def forecast(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("timesfm", "/forecast", "POST", json_body=body)

@app.post("/api/regression")
async def regression(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("eml", "/regress", "POST", json_body=body)

@app.post("/api/deerflow/run")
async def deerflow_run(body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("deerflow", "/run", "POST", json_body=body, timeout=60.0)

@app.get("/api/signal-spectrum/{path:path}")
async def signal_spectrum(path: str, request: Request):
    return await proxy("signal_spectrum", "/" + path, "GET", params=dict(request.query_params))

@app.get("/api/mr/{path:path}")
async def mr(path: str, request: Request):
    return await proxy("mr", "/" + path, "GET", params=dict(request.query_params))

@app.post("/api/agi/{path:path}")
async def agi(path: str, body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("agi", "/" + path, "POST", json_body=body, timeout=60.0)

@app.post("/api/langchain/{path:path}")
async def langchain(path: str, body: Dict[str, Any], x_nwo_wallet: Optional[str] = Header(None)):
    resolve_wallet(x_nwo_wallet, body.get("wallet"))
    return await proxy("langchain", "/" + path, "POST", json_body=body, timeout=60.0)

@app.api_route("/api/robotics-cs/{path:path}", methods=["GET", "POST"])
async def robotics_cs(path: str, request: Request):
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
    return await proxy("robotics_cs", "/" + path, request.method,
                       params=dict(request.query_params), json_body=body)


# =============================================================================
# v0.7.5 · Enactivist feedback channel to CHAINSTATE (Paper V §6.4 · Theorem 9)
# =============================================================================
# Closes the prediction-action-correction loop. Any NWO Robotics workflow that
# executes a robotic action originally initiated by a CHAINSTATE query calls
# this endpoint after the action completes with:
#   * originating_query_hash — the CHAINSTATE receipt content hash from the
#                              query that triggered the action
#   * predicted_outcome      — what the substrate predicted would happen
#   * observed_outcome       — what actually happened
#   * error_metric           — [0.0, 1.0] scalar distance between the two
#   * category_refinements   — optional ontology deltas the outcome suggests
#
# We forward the packet to the CHAINSTATE worker's /enactivist/feedback
# endpoint. When error_metric > ENACTIVIST_THRESHOLD on the worker side
# (default 0.35), the worker anchors an ENACTIVIST_EVENT via the anchor
# microservice, updates reputation, and proposes an ontology delta.
#
# Nothing in the existing NWO Robotics API changes. This is outbound emit
# only. Downstream services and payment flows are untouched.
# =============================================================================

class RoboticsFeedbackBody(BaseModel):
    """
    Prediction-outcome packet for the enactivist channel (Paper V §6.4).
    """
    originating_query_hash: str
    predicted_outcome: Dict[str, Any]
    observed_outcome:  Dict[str, Any]
    error_metric:      float                          # 0.0 = perfect · 1.0 = maximum divergence
    category_refinements: Optional[List[Dict[str, Any]]] = None
    source: Optional[str] = "robotics"                # "robotics" | "neuro"


@app.post("/api/enactivist/emit")
async def enactivist_emit(
    body: RoboticsFeedbackBody,
    x_nwo_wallet: Optional[str] = Header(None),
):
    """
    Forward a prediction-outcome packet from a robotic action to the
    CHAINSTATE worker's enactivist feedback channel.

    Auth: standard wallet resolution (lenient by default, per
    NWO_AUTH_REQUIRED). Body payload is server-side wrapped and signed
    with the Bearer token; the caller does not need to know the token.

    Returns:
      {
        forwarded: bool,
        chainstate_status: int,
        chainstate_response: dict | str,
        source: str,
        exceeded_threshold: bool | None    (populated from worker response)
      }
    """
    # Standard wallet resolution — consistent with the rest of the API
    resolve_wallet(x_nwo_wallet, None)

    # Clamp error_metric to [0, 1]
    err = max(0.0, min(1.0, float(body.error_metric)))

    # Build the payload in the shape the worker expects (see edge-worker.js
    # tomProcessEnactivistFeedback in /home/claude/v075/edge-worker.js)
    payload: Dict[str, Any] = {
        "source":     (body.source or "robotics").strip().lower(),
        "query_hash": body.originating_query_hash,
        "prediction": body.predicted_outcome,
        "outcome":    body.observed_outcome,
        "error":      err,
        "category_hints": body.category_refinements or [],
    }

    headers = {"Content-Type": "application/json"}
    if ENACTIVIST_BEARER:
        headers["Authorization"] = f"Bearer {ENACTIVIST_BEARER}"

    url = CHAINSTATE_WORKER_URL.rstrip("/") + "/enactivist/feedback"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504,
                             detail="CHAINSTATE worker timed out — feedback not delivered")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502,
                             detail=f"CHAINSTATE worker unreachable: {e}")

    # Parse response body — worker returns JSON
    try:
        parsed = resp.json()
    except Exception:
        parsed = {"raw": resp.text[:500]}

    exceeded = parsed.get("exceeded_threshold") if isinstance(parsed, dict) else None

    return {
        "forwarded": 200 <= resp.status_code < 300,
        "chainstate_status": resp.status_code,
        "chainstate_response": parsed,
        "source": payload["source"],
        "exceeded_threshold": exceeded,
    }


# =============================================================================
# Entry point
# =============================================================================
if __name__ == "__main__":
    import uvicorn
    init_pool()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
