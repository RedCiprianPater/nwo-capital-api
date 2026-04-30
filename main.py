# NWO Robotics API - Render Deployment
# FastAPI fallback for nwo.capital PHP APIs

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import sqlite3
import json
import hashlib
import secrets
from datetime import datetime, timedelta
import os

app = FastAPI(
    title="NWO Robotics API",
    description="FastAPI fallback for NWO Robotics ecosystem",
    version="2.0.0"
)

# CORS - Allow HF Space and nwo.capital
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nwo.capital",
        "https://CPater-nwo-capital.hf.space",
        "https://*.hf.space",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DB_PATH = os.getenv("DATABASE_URL", "nwo_api.db")

def init_db():
    """Initialize SQLite database with all tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # API Keys table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT UNIQUE NOT NULL,
            wallet TEXT NOT NULL,
            name TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_suffix TEXT NOT NULL,
            usage_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # Chat messages table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Model usage table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            model_id TEXT NOT NULL,
            calls INTEGER DEFAULT 0,
            cost_eth REAL DEFAULT 0.0,
            last_used TIMESTAMP
        )
    ''')
    
    # IoT Networks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS iot_networks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            network_id TEXT UNIQUE NOT NULL,
            wallet TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            device_count INTEGER DEFAULT 0,
            data_points INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Robots/Agents table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS robots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            robot_id TEXT UNIQUE NOT NULL,
            wallet TEXT NOT NULL,
            name TEXT,
            status TEXT DEFAULT 'offline',
            agent_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    ''')
    
    # Missions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS missions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT UNIQUE NOT NULL,
            wallet TEXT NOT NULL,
            robot_id TEXT NOT NULL,
            type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            waypoints TEXT,
            earnings_eth REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Database initialized")

# Initialize on startup
@app.on_event("startup")
async def startup():
    init_db()

# Helper functions
def generate_id(prefix: str) -> str:
    """Generate unique ID with prefix"""
    return f"{prefix}-{secrets.token_hex(8).upper()}"

def get_db():
    """Database connection generator"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# Request/Response models
class ApiKeyCreate(BaseModel):
    wallet: str
    name: str

class ApiKeyResponse(BaseModel):
    key_id: str
    name: str
    api_key: str  # Only shown once on creation
    key_prefix: str
    created_at: str

class ChatMessage(BaseModel):
    wallet: str
    message: str
    history: Optional[List[Dict]] = []

class ChatResponse(BaseModel):
    response: str
    timestamp: str

class ModelUsage(BaseModel):
    wallet: str
    model_id: str

class IoTNetworkCreate(BaseModel):
    wallet: str
    name: str

class RobotCreate(BaseModel):
    wallet: str
    name: Optional[str] = None
    agent_address: Optional[str] = None

class MissionCreate(BaseModel):
    wallet: str
    robot_id: str
    type: str
    waypoints: Optional[List[Dict]] = []

# ============ API KEY ENDPOINTS ============

@app.post("/api-keys", response_model=ApiKeyResponse)
async def create_api_key(data: ApiKeyCreate, db: sqlite3.Connection = Depends(get_db)):
    """Create new API key for wallet"""
    key_id = generate_id("KEY")
    api_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO api_keys (key_id, wallet, name, api_key_hash, key_prefix, key_suffix)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        key_id,
        data.wallet.lower(),
        data.name,
        key_hash,
        api_key[:8],
        api_key[-4:]
    ))
    db.commit()
    
    return {
        "key_id": key_id,
        "name": data.name,
        "api_key": api_key,  # Only returned once!
        "key_prefix": api_key[:8],
        "created_at": datetime.now().isoformat()
    }

@app.get("/api-keys")
async def list_api_keys(wallet: str, db: sqlite3.Connection = Depends(get_db)):
    """List all API keys for wallet (without full keys)"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT key_id, name, key_prefix, key_suffix, usage_count, created_at, is_active
        FROM api_keys
        WHERE wallet = ? AND is_active = 1
        ORDER BY created_at DESC
    ''', (wallet.lower(),))
    
    keys = []
    for row in cursor.fetchall():
        keys.append({
            "id": row[0],
            "name": row[1],
            "key_prefix": row[2],
            "key_suffix": row[3],
            "usage_count": row[4],
            "created_at": row[5],
            "is_active": bool(row[6])
        })
    
    return {
        "status": "success",
        "count": len(keys),
        "keys": keys
    }

@app.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, wallet: str, db: sqlite3.Connection = Depends(get_db)):
    """Revoke an API key"""
    cursor = db.cursor()
    cursor.execute('''
        UPDATE api_keys SET is_active = 0
        WHERE key_id = ? AND wallet = ?
    ''', (key_id, wallet.lower()))
    db.commit()
    
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Key not found")
    
    return {"status": "success", "message": "Key revoked"}

# ============ CHAT ENDPOINTS ============

@app.post("/chat")
async def chat(data: ChatMessage, db: sqlite3.Connection = Depends(get_db)):
    """Chat with AI assistant"""
    # Store user message
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO chat_messages (wallet, role, content)
        VALUES (?, 'user', ?)
    ''', (data.wallet.lower(), data.message))
    
    # Simple AI response (replace with actual AI integration)
    responses = {
        "create": "I can help you create a robot agent. Please go to the 'Create' tab and connect your wallet.",
        "fleet": "To view your fleet, navigate to the 'Agents' tab after connecting your wallet.",
        "navigate": "I can help navigate your robot. Please provide the destination coordinates.",
        "help": "Available commands: create agent, view fleet, navigate, check status, earnings",
        "earnings": "Your earnings are tracked per wallet and distributed in 35/35/30 split (Guardian/Savings/Operational)."
    }
    
    # Simple keyword matching
    ai_response = "I'm here to help with NWO Robotics. Try asking about creating agents, viewing your fleet, or navigation."
    msg_lower = data.message.lower()
    
    for keyword, response in responses.items():
        if keyword in msg_lower:
            ai_response = response
            break
    
    # Store AI response
    cursor.execute('''
        INSERT INTO chat_messages (wallet, role, content)
        VALUES (?, 'assistant', ?)
    ''', (data.wallet.lower(), ai_response))
    db.commit()
    
    return {
        "status": "success",
        "response": ai_response,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/chat/history")
async def get_chat_history(wallet: str, limit: int = 50, db: sqlite3.Connection = Depends(get_db)):
    """Get chat history for wallet"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT role, content, timestamp
        FROM chat_messages
        WHERE wallet = ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (wallet.lower(), limit))
    
    messages = []
    for row in cursor.fetchall():
        messages.append({
            "role": row[0],
            "content": row[1],
            "timestamp": row[2]
        })
    
    return {
        "status": "success",
        "count": len(messages),
        "messages": list(reversed(messages))
    }

# ============ MODEL USAGE ENDPOINTS ============

@app.get("/model-usage")
async def get_model_usage(wallet: str, db: sqlite3.Connection = Depends(get_db)):
    """Get model usage statistics for wallet"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT model_id, calls, cost_eth, last_used
        FROM model_usage
        WHERE wallet = ?
    ''', (wallet.lower(),))
    
    total_calls = 0
    total_cost = 0.0
    models = []
    
    for row in cursor.fetchall():
        models.append({
            "model_id": row[0],
            "calls": row[1],
            "cost_eth": row[2],
            "last_used": row[3]
        })
        total_calls += row[1]
        total_cost += row[2]
    
    return {
        "status": "success",
        "wallet": wallet,
        "usage": {
            "calls": total_calls,
            "cost": total_cost,
            "models": models
        }
    }

@app.post("/model-usage/track")
async def track_model_usage(data: ModelUsage, db: sqlite3.Connection = Depends(get_db)):
    """Track model usage (called after each API call)"""
    cursor = db.cursor()
    
    # Get model cost
    model_costs = {
        "gpt-4": 0.0001,
        "claude-3": 0.00015,
        "llama-3": 0.00005,
        "timesfm": 0.00002
    }
    cost = model_costs.get(data.model_id, 0.0001)
    
    # Update or insert
    cursor.execute('''
        INSERT INTO model_usage (wallet, model_id, calls, cost_eth, last_used)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(wallet, model_id) DO UPDATE SET
            calls = calls + 1,
            cost_eth = cost_eth + ?,
            last_used = ?
    ''', (data.wallet.lower(), data.model_id, cost, datetime.now(), cost, datetime.now()))
    
    db.commit()
    
    return {"status": "success", "message": "Usage tracked"}

# ============ IOT NETWORKS ENDPOINTS ============

@app.post("/iot-networks")
async def create_iot_network(data: IoTNetworkCreate, db: sqlite3.Connection = Depends(get_db)):
    """Create new IoT network"""
    network_id = generate_id("NET")
    
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO iot_networks (network_id, wallet, name)
        VALUES (?, ?, ?)
    ''', (network_id, data.wallet.lower(), data.name))
    db.commit()
    
    return {
        "status": "success",
        "network_id": network_id,
        "name": data.name,
        "message": "Network created"
    }

@app.get("/iot-networks")
async def list_iot_networks(wallet: str, db: sqlite3.Connection = Depends(get_db)):
    """List all IoT networks for wallet"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT network_id, name, status, device_count, data_points, created_at
        FROM iot_networks
        WHERE wallet = ?
        ORDER BY created_at DESC
    ''', (wallet.lower(),))
    
    networks = []
    for row in cursor.fetchall():
        networks.append({
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "device_count": row[3],
            "data_points": row[4],
            "created_at": row[5]
        })
    
    return {
        "status": "success",
        "count": len(networks),
        "networks": networks
    }

# ============ ROBOT/AGENT ENDPOINTS ============

@app.post("/robots")
async def create_robot(data: RobotCreate, db: sqlite3.Connection = Depends(get_db)):
    """Register new robot/agent"""
    robot_id = generate_id("ROB")
    name = data.name or f"Robot {robot_id[-4:]}"
    
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO robots (robot_id, wallet, name, agent_address)
        VALUES (?, ?, ?, ?)
    ''', (robot_id, data.wallet.lower(), name, data.agent_address))
    db.commit()
    
    return {
        "status": "success",
        "robot_id": robot_id,
        "name": name,
        "message": "Robot registered"
    }

@app.get("/robots")
async def list_robots(wallet: str, db: sqlite3.Connection = Depends(get_db)):
    """List all robots for wallet"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT robot_id, name, status, agent_address, created_at, last_seen
        FROM robots
        WHERE wallet = ?
        ORDER BY created_at DESC
    ''', (wallet.lower(),))
    
    robots = []
    for row in cursor.fetchall():
        robots.append({
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "agent_address": row[3],
            "created_at": row[4],
            "last_seen": row[5]
        })
    
    return {
        "status": "success",
        "count": len(robots),
        "robots": robots
    }

# ============ MISSION ENDPOINTS ============

@app.post("/missions")
async def create_mission(data: MissionCreate, db: sqlite3.Connection = Depends(get_db)):
    """Create new mission"""
    mission_id = generate_id("MIS")
    waypoints_json = json.dumps(data.waypoints) if data.waypoints else "[]"
    
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO missions (mission_id, wallet, robot_id, type, waypoints)
        VALUES (?, ?, ?, ?, ?)
    ''', (mission_id, data.wallet.lower(), data.robot_id, data.type, waypoints_json))
    db.commit()
    
    return {
        "status": "success",
        "mission_id": mission_id,
        "type": data.type,
        "message": "Mission created"
    }

@app.get("/missions")
async def list_missions(wallet: str, status: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    """List missions for wallet"""
    cursor = db.cursor()
    
    if status:
        cursor.execute('''
            SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
            FROM missions
            WHERE wallet = ? AND status = ?
            ORDER BY created_at DESC
        ''', (wallet.lower(), status))
    else:
        cursor.execute('''
            SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
            FROM missions
            WHERE wallet = ?
            ORDER BY created_at DESC
        ''', (wallet.lower(),))
    
    missions = []
    for row in cursor.fetchall():
        missions.append({
            "id": row[0],
            "robot_id": row[1],
            "type": row[2],
            "status": row[3],
            "earnings_eth": row[4],
            "created_at": row[5],
            "completed_at": row[6]
        })
    
    return {
        "status": "success",
        "count": len(missions),
        "missions": missions
    }

# ============ HEALTH & STATUS ============

@app.get("/health")
async def health_check():
    """API health check"""
    return {
        "status": "healthy",
        "service": "nwo-robotics-api",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/")
async def root():
    """API root - list available endpoints"""
    return {
        "service": "NWO Robotics API",
        "version": "2.0.0",
        "description": "FastAPI fallback for NWO Robotics ecosystem",
        "endpoints": {
            "api_keys": "/api-keys (GET, POST, DELETE)",
            "chat": "/chat (POST), /chat/history (GET)",
            "model_usage": "/model-usage (GET), /model-usage/track (POST)",
            "iot_networks": "/iot-networks (GET, POST)",
            "robots": "/robots (GET, POST)",
            "missions": "/missions (GET, POST)",
            "health": "/health"
        }
    }

# Run with: uvicorn main:app --host 0.0.0.0 --port $PORT
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
