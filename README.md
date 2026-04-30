# NWO Robotics API - Render Deployment

FastAPI fallback for NWO Robotics ecosystem.

## Deploy to Render

### Option 1: Deploy from GitHub (Recommended)

1. Push this code to a GitHub repository
2. Go to https://dashboard.render.com/
3. Click "New +" → "Web Service"
4. Connect your GitHub repo
5. Settings:
   - **Name:** `nwo-robotics-api`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Click "Create Web Service"

### Option 2: Deploy from ZIP

1. Go to https://dashboard.render.com/
2. Click "New +" → "Web Service"
3. Select "Upload ZIP"
4. Upload this folder as ZIP
5. Settings as above

## Environment Variables

Set these in Render dashboard:

- `DATABASE_URL` - SQLite path (default: `nwo_api.db`)

## API Endpoints

All endpoints accept `wallet` parameter for wallet-based authentication.

### API Keys
- `POST /api-keys` - Create API key
- `GET /api-keys?wallet=0x...` - List keys
- `DELETE /api-keys/{key_id}?wallet=0x...` - Revoke key

### Chat
- `POST /chat` - Send message
- `GET /chat/history?wallet=0x...` - Get history

### Model Usage
- `GET /model-usage?wallet=0x...` - Get usage stats
- `POST /model-usage/track` - Track usage

### IoT Networks
- `POST /iot-networks` - Create network
- `GET /iot-networks?wallet=0x...` - List networks

### Robots
- `POST /robots` - Register robot
- `GET /robots?wallet=0x...` - List robots

### Missions
- `POST /missions` - Create mission
- `GET /missions?wallet=0x...` - List missions

## CORS

Configured to accept requests from:
- https://nwo.capital
- https://*.hf.space
- http://localhost:3000

## Database

SQLite with tables:
- `api_keys` - API key management
- `chat_messages` - Chat history
- `model_usage` - Model usage tracking
- `iot_networks` - IoT network registry
- `robots` - Robot/agent registry
- `missions` - Mission tracking

Data persists in SQLite file.
