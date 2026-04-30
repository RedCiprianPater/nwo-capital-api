# NWO Robotics API - Flask Version
# Simplified fallback for Render deployment

from flask import Flask, request, jsonify, g
from flask_cors import CORS
import sqlite3
import json
import hashlib
import secrets
from datetime import datetime
import os

app = Flask(__name__)

# Enable CORS for all origins
CORS(app, resources={
    r"/api/*": {
        "origins": ["*"],
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"]
    }
})

# Database setup
DB_PATH = os.getenv("DATABASE_URL", "nwo_api.db")

def get_db():
    """Get database connection"""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    """Close database connection"""
    if 'db' in g:
        g.db.close()

def init_db():
    """Initialize database tables"""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        
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
                is_active INTEGER DEFAULT 1
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
                last_used TIMESTAMP,
                UNIQUE(wallet, model_id)
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
        
        # Robots table
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
        
        db.commit()
        print("✅ Database initialized")

# Initialize on startup
with app.app_context():
    init_db()

def generate_id(prefix):
    """Generate unique ID"""
    return f"{prefix}-{secrets.token_hex(8).upper()}"

# ============ API ROUTES ============

@app.route('/')
def index():
    """API root"""
    return jsonify({
        "service": "NWO Robotics API",
        "version": "2.0.0-flask",
        "status": "running",
        "endpoints": [
            "/api/health",
            "/api/api-keys",
            "/api/chat",
            "/api/model-usage",
            "/api/iot-networks",
            "/api/robots",
            "/api/missions"
        ]
    })

@app.route('/api/health')
def health():
    """Health check"""
    return jsonify({
        "status": "healthy",
        "service": "nwo-robotics-api",
        "version": "2.0.0-flask",
        "timestamp": datetime.now().isoformat()
    })

# API Keys
@app.route('/api/api-keys', methods=['GET', 'POST'])
def api_keys():
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        data = request.get_json()
        wallet = data.get('wallet', '').lower()
        name = data.get('name', 'Default Key')
        
        key_id = generate_id("KEY")
        api_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        
        cursor.execute('''
            INSERT INTO api_keys (key_id, wallet, name, api_key_hash, key_prefix, key_suffix)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (key_id, wallet, name, key_hash, api_key[:8], api_key[-4:]))
        db.commit()
        
        return jsonify({
            "status": "success",
            "key_id": key_id,
            "name": name,
            "api_key": api_key,
            "key_prefix": api_key[:8],
            "created_at": datetime.now().isoformat()
        })
    
    else:  # GET
        wallet = request.args.get('wallet', '').lower()
        cursor.execute('''
            SELECT key_id, name, key_prefix, key_suffix, usage_count, created_at, is_active
            FROM api_keys
            WHERE wallet = ? AND is_active = 1
            ORDER BY created_at DESC
        ''', (wallet,))
        
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
        
        return jsonify({"status": "success", "count": len(keys), "keys": keys})

@app.route('/api/api-keys/<key_id>', methods=['DELETE'])
def revoke_api_key(key_id):
    """Revoke an API key (sets is_active=0). Wallet must match owner."""
    db = get_db()
    cursor = db.cursor()
 
    wallet = request.args.get('wallet', '').lower()
    if not wallet:
        return jsonify({"status": "error", "error": "wallet parameter required"}), 400
 
    cursor.execute('''
        UPDATE api_keys SET is_active = 0
        WHERE key_id = ? AND wallet = ? AND is_active = 1
    ''', (key_id, wallet))
    db.commit()
 
    if cursor.rowcount == 0:
        return jsonify({"status": "error", "error": "Key not found or already revoked"}), 404
 
    return jsonify({"status": "success", "message": "Key revoked", "key_id": key_id})

# Chat
@app.route('/api/chat', methods=['POST'])
def chat():
    db = get_db()
    cursor = db.cursor()
    
    data = request.get_json()
    wallet = data.get('wallet', '').lower()
    message = data.get('message', '')
    
    # Store user message
    cursor.execute('''
        INSERT INTO chat_messages (wallet, role, content)
        VALUES (?, 'user', ?)
    ''', (wallet, message))
    
    # Simple AI response
    responses = {
        "create": "I can help you create a robot agent. Please go to the 'Create' tab and connect your wallet.",
        "fleet": "To view your fleet, navigate to the 'Agents' tab after connecting your wallet.",
        "help": "Available commands: create agent, view fleet, navigate, check status, earnings"
    }
    
    ai_response = "I'm here to help with NWO Robotics. Try asking about creating agents, viewing your fleet, or navigation."
    msg_lower = message.lower()
    
    for keyword, response in responses.items():
        if keyword in msg_lower:
            ai_response = response
            break
    
    # Store AI response
    cursor.execute('''
        INSERT INTO chat_messages (wallet, role, content)
        VALUES (?, 'assistant', ?)
    ''', (wallet, ai_response))
    db.commit()
    
    return jsonify({
        "status": "success",
        "response": ai_response,
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/chat/history', methods=['GET'])
def chat_history():
    db = get_db()
    cursor = db.cursor()
    
    wallet = request.args.get('wallet', '').lower()
    limit = request.args.get('limit', 50, type=int)
    
    cursor.execute('''
        SELECT role, content, timestamp
        FROM chat_messages
        WHERE wallet = ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (wallet, limit))
    
    messages = [{"role": row[0], "content": row[1], "timestamp": row[2]} 
                for row in cursor.fetchall()]
    
    return jsonify({
        "status": "success",
        "count": len(messages),
        "messages": list(reversed(messages))
    })

# Model Usage
@app.route('/api/model-usage', methods=['GET'])
def model_usage():
    db = get_db()
    cursor = db.cursor()
    
    wallet = request.args.get('wallet', '').lower()
    cursor.execute('''
        SELECT model_id, calls, cost_eth, last_used
        FROM model_usage
        WHERE wallet = ?
    ''', (wallet,))
    
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
    
    return jsonify({
        "status": "success",
        "wallet": wallet,
        "usage": {"calls": total_calls, "cost": total_cost, "models": models}
    })

# IoT Networks
@app.route('/api/iot-networks', methods=['GET', 'POST'])
def iot_networks():
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        data = request.get_json()
        wallet = data.get('wallet', '').lower()
        name = data.get('name', 'New Network')
        
        network_id = generate_id("NET")
        cursor.execute('''
            INSERT INTO iot_networks (network_id, wallet, name)
            VALUES (?, ?, ?)
        ''', (network_id, wallet, name))
        db.commit()
        
        return jsonify({
            "status": "success",
            "network_id": network_id,
            "name": name
        })
    
    else:  # GET
        wallet = request.args.get('wallet', '').lower()
        cursor.execute('''
            SELECT network_id, name, status, device_count, data_points, created_at
            FROM iot_networks
            WHERE wallet = ?
            ORDER BY created_at DESC
        ''', (wallet,))
        
        networks = [{
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "device_count": row[3],
            "data_points": row[4],
            "created_at": row[5]
        } for row in cursor.fetchall()]
        
        return jsonify({"status": "success", "count": len(networks), "networks": networks})

# Robots
@app.route('/api/robots', methods=['GET', 'POST'])
def robots():
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        data = request.get_json()
        wallet = data.get('wallet', '').lower()
        name = data.get('name') or f"Robot {generate_id('')[-4:]}"
        agent_address = data.get('agent_address')
        
        robot_id = generate_id("ROB")
        cursor.execute('''
            INSERT INTO robots (robot_id, wallet, name, agent_address)
            VALUES (?, ?, ?, ?)
        ''', (robot_id, wallet, name, agent_address))
        db.commit()
        
        return jsonify({
            "status": "success",
            "robot_id": robot_id,
            "name": name
        })
    
    else:  # GET
        wallet = request.args.get('wallet', '').lower()
        cursor.execute('''
            SELECT robot_id, name, status, agent_address, created_at, last_seen
            FROM robots
            WHERE wallet = ?
            ORDER BY created_at DESC
        ''', (wallet,))
        
        robots_list = [{
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "agent_address": row[3],
            "created_at": row[4],
            "last_seen": row[5]
        } for row in cursor.fetchall()]
        
        return jsonify({"status": "success", "count": len(robots_list), "robots": robots_list})

# Missions
@app.route('/api/missions', methods=['GET', 'POST'])
def missions():
    db = get_db()
    cursor = db.cursor()
    
    if request.method == 'POST':
        data = request.get_json()
        wallet = data.get('wallet', '').lower()
        robot_id = data.get('robot_id')
        mission_type = data.get('type')
        waypoints = json.dumps(data.get('waypoints', []))
        
        mission_id = generate_id("MIS")
        cursor.execute('''
            INSERT INTO missions (mission_id, wallet, robot_id, type, waypoints)
            VALUES (?, ?, ?, ?, ?)
        ''', (mission_id, wallet, robot_id, mission_type, waypoints))
        db.commit()
        
        return jsonify({
            "status": "success",
            "mission_id": mission_id,
            "type": mission_type
        })
    
    else:  # GET
        wallet = request.args.get('wallet', '').lower()
        status_filter = request.args.get('status')
        
        if status_filter:
            cursor.execute('''
                SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
                FROM missions
                WHERE wallet = ? AND status = ?
                ORDER BY created_at DESC
            ''', (wallet, status_filter))
        else:
            cursor.execute('''
                SELECT mission_id, robot_id, type, status, earnings_eth, created_at, completed_at
                FROM missions
                WHERE wallet = ?
                ORDER BY created_at DESC
            ''', (wallet,))
        
        missions_list = [{
            "id": row[0],
            "robot_id": row[1],
            "type": row[2],
            "status": row[3],
            "earnings_eth": row[4],
            "created_at": row[5],
            "completed_at": row[6]
        } for row in cursor.fetchall()]
        
        return jsonify({"status": "success", "count": len(missions_list), "missions": missions_list})

# Run the app
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
