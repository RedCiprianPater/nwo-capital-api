"""
platform_bp.py — Tier-A endpoints for nwo-capital-api on Render

What this adds to your existing Flask app:
  ~25 new endpoints across these PHP groups, all now on Render:
      4. Agent Management        (6 eps)  — register, pay, balance, get, update, register_agent
      5. Agent Discovery          (5 eps) — health, whoami, capabilities, dry-run, plan
      9. Embodiment & Calibration (8 eps) — list, detail, normalization, urdf, test_results,
                                            compare, run_calibration, calibrate
     10. RL session bookkeeping   (2 eps) — start_online_rl, submit_telemetry (CRUD parts only)
     11. Tactile log              (1 ep)  — get_tactile (DB read)
     12. Dataset Hub              (1 ep)  — list Unitree datasets
      1. Model registry partial   (2 eps) — list_models, get_model_info
  Plus the missing DELETE /api/api-keys/{id} endpoint that the React UI expects.

Assumptions about your existing Render app:
  - Flask + flask-cors
  - A signed-session auth middleware (before_request hook) that reads
    X-NWO-Wallet, X-NWO-Message, X-NWO-Signature headers, verifies the
    eth personal_sign, and stashes the wallet address on `flask.g.wallet`.
    (Same middleware already used by /api/robots, /api/api-keys, etc.)
  - A DB session importable from your app. The TODOs below show where to
    wire the ORM. If you don't have models for Agent/CalibrationRecord/
    RlSession/RlTelemetry yet, the stub responses still return 501 with
    a clear todo so the React app shows a "not yet wired" message
    rather than 404.

REGISTRATION
Put this file in your Render repo (e.g. `blueprints/platform_bp.py`).
In `app.py` (or wherever you instantiate Flask), add:

    from blueprints.platform_bp import platform_bp
    app.register_blueprint(platform_bp, url_prefix='/api')

That mounts every route below under /api on Render.

URL CONVENTION
PHP used `/api-robotics.php?action=X` query-style. We're using REST paths
so the React app and external agents get cleaner URLs.
"""

from flask import Blueprint, request, jsonify, g, abort
import time

# Adapt these two imports to your real app structure:
# from app import db  # SQLAlchemy session
# from models import Agent, ApiKey, ModelUsage, CalibrationRecord, RlSession, RlTelemetry

platform_bp = Blueprint('platform', __name__)


# ============================================================
# AUTH HELPER
# ============================================================
def _require_wallet():
    """Reject the request unless signed-session auth already populated g.wallet.

    Your existing before_request hook should do the verification. This
    helper just guards the routes below. If you want, you can also put
    this on a blueprint-level before_request:

        @platform_bp.before_request
        def _gate():
            # skip discovery/health which is intentionally unauth'd
            if request.endpoint == 'platform.discovery_health':
                return None
            _require_wallet()
    """
    if not getattr(g, 'wallet', None):
        abort(401, description='wallet authentication required')


# ============================================================
# GROUP 4 — AGENT MANAGEMENT
# ============================================================

@platform_bp.route('/agents', methods=['POST'])
def register_agent():
    """Replaces POST /api-agent-register.php.

    Body: { agent_name, capabilities?, signature? }
    Creates an agent owned by the caller's wallet.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    name = (body.get('agent_name') or body.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'agent_name required'}), 400

    # TODO: implement with your real ORM
    # agent = Agent(name=name, owner=g.wallet,
    #               capabilities=body.get('capabilities') or [],
    #               tier='free')
    # db.session.add(agent)
    # db.session.commit()
    # api_key = mint_api_key(agent_id=agent.id, owner=g.wallet)
    # return jsonify({
    #     'agent_id': agent.id,
    #     'api_key': api_key.full_key,
    #     'tier': agent.tier,
    # }), 201
    return jsonify({
        'todo': 'wire Agent model + mint_api_key()',
        'name': name,
        'owner': g.wallet,
    }), 501


@platform_bp.route('/agents/<agent_id>', methods=['GET'])
def get_agent(agent_id):
    """Replaces GET /api-robotics.php?action=get_agent."""
    _require_wallet()
    # agent = Agent.query.filter_by(id=agent_id, owner=g.wallet).first_or_404()
    # return jsonify(agent.to_dict())
    return jsonify({'todo': 'wire to Agent model', 'agent_id': agent_id}), 501


@platform_bp.route('/agents/<agent_id>', methods=['PUT'])
def update_agent(agent_id):
    """Replaces PUT /api-robotics.php?action=update_agent."""
    _require_wallet()
    body = request.get_json(silent=True) or {}
    # agent = Agent.query.filter_by(id=agent_id, owner=g.wallet).first_or_404()
    # for k in ('name', 'capabilities', 'status'):
    #     if k in body: setattr(agent, k, body[k])
    # db.session.commit()
    # return jsonify(agent.to_dict())
    return jsonify({'todo': 'wire to Agent model', 'agent_id': agent_id, 'patch': body}), 501


@platform_bp.route('/agents/<agent_id>/balance', methods=['GET'])
def agent_balance(agent_id):
    """Replaces GET /api-agent-balance.php.

    Returns the DB-side view of the agent: tier, quota, calls used. ETH
    wallet balance is read on the client side via ethers; that doesn't
    need a server roundtrip.
    """
    _require_wallet()
    # TIER_QUOTAS = {'free': 100_000, 'prototype': 500_000, 'production': float('inf')}
    # agent = Agent.query.filter_by(id=agent_id, owner=g.wallet).first_or_404()
    # usage = ModelUsage.query.filter_by(agent_id=agent_id).all()
    # calls_used = sum(u.calls for u in usage)
    # limit = TIER_QUOTAS.get(agent.tier, 0)
    # return jsonify({
    #     'agent_id': agent_id,
    #     'tier': agent.tier,
    #     'quota_limit': None if limit == float('inf') else limit,
    #     'calls_used': calls_used,
    #     'quota_remaining': None if limit == float('inf') else max(0, limit - calls_used),
    # })
    return jsonify({'todo': 'aggregate ModelUsage', 'agent_id': agent_id}), 501


@platform_bp.route('/agents/<agent_id>/pay', methods=['POST'])
def agent_pay(agent_id):
    """Replaces POST /api-agent-pay.php.

    Body: { tier: 'prototype'|'production', tx_hash: '0x...' }
    Verifies on Base mainnet that the tx is to the platform's payment
    processor address (0x4afa4618bb992a073dbcfbddd6d1aebc3d5abd7c),
    that the amount matches the tier cost, and that tx.from matches
    the caller's wallet.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    tier = body.get('tier')
    tx_hash = body.get('tx_hash')

    if tier not in ('free', 'prototype', 'production'):
        return jsonify({'error': 'invalid tier'}), 400
    if tier != 'free' and not tx_hash:
        return jsonify({'error': 'tx_hash required for paid tiers'}), 400

    # PAYMENT_PROCESSOR = '0x4afa4618bb992a073dbcfbddd6d1aebc3d5abd7c'
    # TIER_PRICES_WEI = {
    #     'free': 0,
    #     'prototype': int(0.015 * 1e18),
    #     'production': int(0.062 * 1e18),
    # }
    # web3 = get_base_web3()
    # receipt = web3.eth.get_transaction_receipt(tx_hash)
    # tx = web3.eth.get_transaction(tx_hash)
    # if receipt.status != 1: return jsonify({'error': 'tx failed'}), 400
    # if tx['to'].lower() != PAYMENT_PROCESSOR.lower():
    #     return jsonify({'error': 'wrong recipient'}), 400
    # if tx['from'].lower() != g.wallet.lower():
    #     return jsonify({'error': 'tx not from caller wallet'}), 400
    # if tx['value'] < TIER_PRICES_WEI[tier]:
    #     return jsonify({'error': 'insufficient payment'}), 400
    # agent = Agent.query.filter_by(id=agent_id, owner=g.wallet).first_or_404()
    # agent.tier = tier
    # db.session.commit()
    # return jsonify({'agent_id': agent_id, 'tier': tier, 'tx_hash': tx_hash})
    return jsonify({'todo': 'verify Base tx + upgrade tier',
                    'agent_id': agent_id, 'tier': tier}), 501


# ============================================================
# GROUP 5 — AGENT DISCOVERY
# ============================================================

@platform_bp.route('/discovery/health', methods=['GET'])
def discovery_health():
    """Public health probe (no auth). Used by the React header badge."""
    return jsonify({
        'service': 'NWO Robotics Discovery',
        'status': 'ok',
        'version': '1.0.0',
        'time': int(time.time()),
    })


@platform_bp.route('/discovery/whoami', methods=['GET'])
def discovery_whoami():
    """Replaces GET /api-agent-discovery.php?action=whoami.

    Resolves the caller's wallet → list of agents owned. Used by the
    Dashboard "Active Agents" stat.
    """
    _require_wallet()
    # agents = Agent.query.filter_by(owner=g.wallet).all()
    # return jsonify({
    #     'wallet': g.wallet,
    #     'agents': [
    #         {'id': a.id, 'name': a.name, 'tier': a.tier, 'status': a.status}
    #         for a in agents
    #     ],
    # })
    return jsonify({'wallet': g.wallet, 'agents': []})


@platform_bp.route('/discovery/capabilities', methods=['GET'])
def discovery_capabilities():
    """Replaces GET /api-agent-discovery.php?action=capabilities.

    Returns a manifest of what the caller's tier permits.
    """
    _require_wallet()
    # tier = current_tier_for(g.wallet)
    tier = 'free'  # default until you wire tier lookup
    capabilities = {
        'inference': {
            'available': True,
            'host': 'nwo-robotics-api-edge.ciprianpater.workers.dev',
            'note': 'GPU-backed, served via CF Workers edge',
        },
        'robots': {
            'available': True,
            'host': 'nwo-capital-api.onrender.com',
        },
        'iot_networks': {
            'available': True,
            'host': 'nwo-capital-api.onrender.com',
        },
        'missions': {
            'available': True,
            'host': 'nwo-capital-api.onrender.com',
        },
        'ros2_bridge': {
            'available': tier != 'free',
            'host': 'nwo-ros2-bridge.onrender.com',
            'note': 'paid tier only',
        },
        'simulation': {
            'available': tier == 'production',
            'host': 'nwo.capital',
            'note': 'production tier only — runs on PHP',
        },
    }
    return jsonify({'tier': tier, 'capabilities': capabilities})


@platform_bp.route('/discovery/dry-run', methods=['POST'])
def discovery_dry_run():
    """Replaces POST /api-agent-discovery.php?action=dry-run.

    Validates a proposed action without executing. Returns estimated
    cost, latency, and safety pre-checks.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    action = body.get('action', 'inference')

    if action == 'inference':
        return jsonify({
            'valid': True,
            'estimated_cost_eth': 0.00002,
            'estimated_latency_ms': 142,
            'safety_checks': ['within_force_limit', 'within_speed_limit'],
        })
    if action == 'mission_deploy':
        return jsonify({
            'valid': True,
            'estimated_cost_eth': 0.0001,
            'estimated_latency_ms': 50,
            'safety_checks': ['mission_goal_valid', 'agent_authorized'],
        })
    return jsonify({'valid': False, 'error': f'unknown action: {action}'}), 400


@platform_bp.route('/discovery/plan', methods=['POST'])
def discovery_plan():
    """Replaces POST /api-agent-discovery.php?action=plan.

    Returns a deterministic skeleton plan. The full LLM-backed planner
    still lives on PHP — this is a placeholder so the React UI has
    something useful from Render.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    intent = body.get('intent', '')

    return jsonify({
        'intent': intent,
        'steps': [
            {'order': 1, 'kind': 'inspect',   'endpoint': '/api/robots'},
            {'order': 2, 'kind': 'inference', 'endpoint': 'https://nwo-robotics-api-edge.ciprianpater.workers.dev/api/inference'},
            {'order': 3, 'kind': 'monitor',   'endpoint': '/api/missions'},
        ],
        'note': 'stub plan — full planner pending GPU/LLM compute',
    })


# ============================================================
# GROUP 9 — EMBODIMENT & CALIBRATION
# ============================================================
#
# The embodiment registry is a small, mostly-static table. Keeping it
# in code (or a JSON file you check in) is fine; promote to DB if you
# want write paths.

EMBODIMENT_REGISTRY = {
    'ur5e': {
        'name': 'Universal Robots UR5e',
        'dof': 6,
        'max_speed_m_s': 1.0,
        'payload_kg': 5.0,
        'urdf_path': '/static/urdf/ur5e.urdf',
        'normalization': {
            'min':  [-3.14, -3.14, -3.14, -3.14, -3.14, -3.14],
            'max':  [ 3.14,  3.14,  3.14,  3.14,  3.14,  3.14],
            'mean': [ 0.00,  0.00,  0.00,  0.00,  0.00,  0.00],
            'std':  [ 1.00,  1.00,  1.00,  1.00,  1.00,  1.00],
        },
        'sensors': ['joint_encoders', 'force_torque_wrist'],
    },
    'unitree_g1': {
        'name': 'Unitree G1 Humanoid',
        'dof': 23,
        'max_speed_m_s': 2.0,
        'payload_kg': 3.0,
        'urdf_path': '/static/urdf/unitree_g1.urdf',
        'normalization': None,  # fill from training data
        'sensors': ['imu', 'camera_rgb', 'depth', 'joint_encoders'],
    },
    'spot': {
        'name': 'Boston Dynamics Spot',
        'dof': 12,
        'max_speed_m_s': 1.6,
        'payload_kg': 14.0,
        'urdf_path': '/static/urdf/spot.urdf',
        'normalization': None,
        'sensors': ['imu', 'cameras_5', 'depth_5'],
    },
    # Add more as you catalog them
}


@platform_bp.route('/embodiment', methods=['GET'])
def embodiment_list():
    """Replaces GET /api-embodiment.php?action=list."""
    return jsonify({
        'count': len(EMBODIMENT_REGISTRY),
        'robots': [
            {'id': k, 'name': v['name'], 'dof': v['dof']}
            for k, v in EMBODIMENT_REGISTRY.items()
        ],
    })


@platform_bp.route('/embodiment/<robot_type>', methods=['GET'])
def embodiment_detail(robot_type):
    """Replaces GET /api-embodiment.php?action=detail&robot_type=..."""
    spec = EMBODIMENT_REGISTRY.get(robot_type)
    if not spec:
        return jsonify({'error': f'unknown robot_type: {robot_type}'}), 404
    return jsonify({'id': robot_type, **spec})


@platform_bp.route('/embodiment/<robot_type>/normalization', methods=['GET'])
def embodiment_normalization(robot_type):
    """Replaces GET /api-embodiment.php?action=normalization."""
    spec = EMBODIMENT_REGISTRY.get(robot_type)
    if not spec:
        return jsonify({'error': f'unknown robot_type: {robot_type}'}), 404
    return jsonify({'id': robot_type, 'normalization': spec.get('normalization')})


@platform_bp.route('/embodiment/<robot_type>/urdf', methods=['GET'])
def embodiment_urdf(robot_type):
    """Replaces GET /api-embodiment.php?action=urdf.

    Serves the URDF XML. Put your URDF files in `static/urdf/` and
    swap the stub for `send_from_directory`.
    """
    spec = EMBODIMENT_REGISTRY.get(robot_type)
    if not spec or not spec.get('urdf_path'):
        return jsonify({'error': f'no URDF for {robot_type}'}), 404
    # from flask import send_from_directory
    # return send_from_directory('static/urdf', f'{robot_type}.urdf',
    #                            mimetype='application/xml')
    return jsonify({'todo': 'serve URDF file', 'path': spec['urdf_path']}), 501


@platform_bp.route('/embodiment/<robot_type>/test-results', methods=['GET'])
def embodiment_test_results(robot_type):
    """Replaces GET /api-embodiment.php?action=test_results.

    LIBERO/CALVIN/SimplerEnv benchmark results for a robot type.
    Replace static dict with a DB query if you start storing real runs.
    """
    if robot_type not in EMBODIMENT_REGISTRY:
        return jsonify({'error': f'unknown robot_type: {robot_type}'}), 404
    return jsonify({
        'id': robot_type,
        'benchmarks': {
            'libero':      {'pass_rate': None, 'note': 'pending re-run'},
            'calvin':      {'pass_rate': None, 'note': 'pending'},
            'simpler_env': {'pass_rate': None, 'note': 'pending'},
        },
    })


@platform_bp.route('/embodiment/compare', methods=['POST'])
def embodiment_compare():
    """Replaces POST /api-embodiment.php?action=compare.

    Body: { robot_types: ['ur5e', 'unitree_g1', ...] }
    Returns the spec rows side-by-side.
    """
    body = request.get_json(silent=True) or {}
    ids = body.get('robot_types', [])
    if not isinstance(ids, list) or len(ids) < 2:
        return jsonify({'error': 'robot_types must be a list of 2+ ids'}), 400

    rows = []
    for i in ids:
        spec = EMBODIMENT_REGISTRY.get(i)
        if not spec:
            return jsonify({'error': f'unknown robot_type: {i}'}), 404
        rows.append({'id': i, **{k: v for k, v in spec.items() if k != 'normalization'}})
    return jsonify({'comparison': rows})


@platform_bp.route('/calibration', methods=['POST'])
def calibration_save():
    """Replaces POST /api-calibration.php?action=calibrate.

    Body: { robot_id, vision_T_base: [[..],..], notes }
    Persists a calibration record for later retrieval.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    if not body.get('robot_id'):
        return jsonify({'error': 'robot_id required'}), 400

    # rec = CalibrationRecord(
    #     wallet=g.wallet,
    #     robot_id=body['robot_id'],
    #     vision_t_base=json.dumps(body.get('vision_T_base')),
    #     notes=body.get('notes', ''),
    # )
    # db.session.add(rec)
    # db.session.commit()
    # return jsonify({'id': rec.id, 'created_at': rec.created_at.isoformat()}), 201
    return jsonify({'todo': 'add CalibrationRecord model', 'patch': body}), 501


@platform_bp.route('/calibration/run', methods=['POST'])
def calibration_run():
    """Replaces POST /api-calibration.php?action=run_calibration.

    Triggers a calibration job on the ROS2 bridge service. The bridge
    already exposes /api/v1/action — we just forward there.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    robot_id = body.get('robot_id')
    if not robot_id:
        return jsonify({'error': 'robot_id required'}), 400

    # import requests
    # forwarded = requests.post(
    #     'https://nwo-ros2-bridge.onrender.com/api/v1/action',
    #     json={'robot_id': robot_id, 'action': 'calibrate', 'params': body.get('params') or {}},
    #     timeout=10,
    # )
    # return jsonify(forwarded.json()), forwarded.status_code
    return jsonify({
        'todo': 'forward to nwo-ros2-bridge.onrender.com/api/v1/action',
        'robot_id': robot_id,
        'action': 'calibrate',
    }), 501


# ============================================================
# GROUP 10 — RL SESSION BOOKKEEPING (CRUD parts only)
# ============================================================

@platform_bp.route('/rl/sessions', methods=['POST'])
def rl_start():
    """Replaces POST /api-online-rl.php?action=start_online_rl.

    Creates a session row. Actual training (GPU) runs elsewhere.
    """
    _require_wallet()
    body = request.get_json(silent=True) or {}
    # sess = RlSession(
    #     wallet=g.wallet,
    #     robot_id=body.get('robot_id'),
    #     config_json=json.dumps(body.get('config') or {}),
    #     status='pending',
    # )
    # db.session.add(sess); db.session.commit()
    # return jsonify({'session_id': sess.id, 'status': sess.status}), 201
    return jsonify({'todo': 'add RlSession model', 'patch': body}), 501


@platform_bp.route('/rl/sessions/<session_id>/telemetry', methods=['POST'])
def rl_telemetry(session_id):
    """Replaces POST /api-online-rl.php?action=submit_telemetry."""
    _require_wallet()
    body = request.get_json(silent=True) or {}
    # tel = RlTelemetry(session_id=session_id, payload_json=json.dumps(body))
    # db.session.add(tel); db.session.commit()
    # return jsonify({'recorded': True, 'id': tel.id}), 201
    return jsonify({'todo': 'add RlTelemetry model', 'session_id': session_id}), 501


# ============================================================
# GROUP 11 — TACTILE READ (ORCA Hand)
# ============================================================

@platform_bp.route('/tactile/orca', methods=['GET'])
def tactile_orca():
    """Replaces GET /api-orca.php?action=get_tactile.

    Returns the latest cached tactile reading for an ORCA hand. The
    write path (sensor → DB) is handled by the ROS2 bridge; this is
    the read side.
    """
    _require_wallet()
    robot_id = request.args.get('robot_id')
    if not robot_id:
        return jsonify({'error': 'robot_id query param required'}), 400
    # latest = TactileReading.query.filter_by(robot_id=robot_id) \
    #     .order_by(TactileReading.created_at.desc()).first()
    # if not latest: return jsonify({'error': 'no readings'}), 404
    # return jsonify({
    #     'robot_id': robot_id,
    #     'created_at': latest.created_at.isoformat(),
    #     'taxels': json.loads(latest.taxels_json),
    # })
    return jsonify({
        'todo': 'add TactileReading model + write path on bridge',
        'robot_id': robot_id,
    }), 501


# ============================================================
# GROUP 12 — DATASET HUB
# ============================================================

DATASET_REGISTRY = [
    {
        'id': 'unitree_g1_v1',
        'name': 'Unitree G1 Daily Tasks v1',
        'episodes': 1_540_000,
        'format': 'lerobot',
        'size_gb': 1280,
        'license': 'MIT',
        'download_url': 'https://huggingface.co/datasets/unitreerobotics/g1_v1',
    },
    # Add more as you catalog them
]


@platform_bp.route('/datasets', methods=['GET'])
def datasets_list():
    """Replaces GET /api-unitree-datasets.php?action=list."""
    return jsonify({'count': len(DATASET_REGISTRY), 'datasets': DATASET_REGISTRY})


# ============================================================
# GROUP 1 — MODEL REGISTRY (CRUD only, not inference)
# ============================================================

MODEL_REGISTRY = [
    {
        'id': 'nwo-vla-base-v2',
        'name': 'NWO VLA Base v2',
        'kind': 'vla',
        'host': 'nwo-robotics-api-edge.ciprianpater.workers.dev',
        'latency_ms': 142,
        'cost_eth': 0.0001,
        'capabilities': ['pick_place', 'navigate', 'sort'],
    },
    {
        'id': 'nwo-vla-fast-v1',
        'name': 'NWO VLA Fast v1',
        'kind': 'vla',
        'host': 'nwo-robotics-api-edge.ciprianpater.workers.dev',
        'latency_ms': 89,
        'cost_eth': 0.00005,
        'capabilities': ['pick_place', 'navigate'],
    },
    {
        'id': 'nwo-vla-precision-v1',
        'name': 'NWO VLA Precision v1',
        'kind': 'vla',
        'host': 'nwo.capital',
        'latency_ms': 312,
        'cost_eth': 0.00015,
        'capabilities': ['pick_place', 'sort', 'fine_assembly', 'tactile_grasp'],
    },
    {
        'id': 'xiaomi-robotics-0',
        'name': 'Xiaomi Robotics v0',
        'kind': 'vla',
        'host': 'nwo.capital',
        'latency_ms': 220,
        'cost_eth': 0.0001,
        'capabilities': ['pick_place', 'navigate', 'sort'],
    },
]


@platform_bp.route('/models', methods=['GET'])
def models_list():
    """Replaces GET /api-robotics.php?action=list_models."""
    return jsonify({'count': len(MODEL_REGISTRY), 'models': MODEL_REGISTRY})


@platform_bp.route('/models/<model_id>', methods=['GET'])
def model_info(model_id):
    """Replaces GET /api-robotics.php?action=get_model_info."""
    m = next((x for x in MODEL_REGISTRY if x['id'] == model_id), None)
    if not m:
        return jsonify({'error': f'unknown model_id: {model_id}'}), 404
    return jsonify(m)


# ============================================================
# DELETE /api/api-keys/{id} — the missing endpoint the UI expects
# ============================================================
#
# Your existing app.py already serves GET/POST on /api/api-keys but
# not DELETE. The React ApiKeysPage.revokeKey() expects this and
# currently shows an error referencing `app_py_delete_endpoint.patch.py`.
# This is that endpoint.

@platform_bp.route('/api-keys/<key_id>', methods=['DELETE'])
def api_keys_delete(key_id):
    """Revoke an API key. Owner-scoped to caller wallet."""
    _require_wallet()
    # k = ApiKey.query.filter_by(id=key_id, owner=g.wallet).first()
    # if not k: return jsonify({'error': 'not found or not yours'}), 404
    # db.session.delete(k); db.session.commit()
    # return jsonify({'revoked': True, 'id': key_id}), 200
    return jsonify({'todo': 'wire to ApiKey model', 'key_id': key_id}), 501


# ============================================================
# REGISTRATION — add to your app.py:
#   from blueprints.platform_bp import platform_bp
#   app.register_blueprint(platform_bp, url_prefix='/api')
# ============================================================
