"""
NWO Capital — Wallet Signature Authentication Patch
====================================================

Adds session-signature verification to the Flask app on Render.

WHAT THIS DOES
--------------
The React app at cpater-nwo-capital.hf.space now signs a session message once
when the user connects their wallet. Every subsequent API request includes:

    X-NWO-Wallet:    0x... (lowercase)
    X-NWO-Message:   the multi-line message that was signed
    X-NWO-Signature: 0x... (signature from MetaMask personal_sign)

This patch verifies those headers using ecrecover (eth_account) and rejects
requests where:
  - signature doesn't match the claimed wallet
  - origin in the message doesn't match the expected origin
  - issued_at/expires timestamps are malformed or expired
  - any required header is missing

DEPLOY STEPS
------------
1. Add to requirements.txt:
       eth-account>=0.10.0

2. Set env vars on Render:
       NWO_AUTH_REQUIRED=false       (start permissive, flip to true after testing)
       NWO_ALLOWED_ORIGIN=https://cpater-nwo-capital.hf.space
       (multiple origins comma-separated if you need staging too)

3. Append the code BELOW into your existing app.py (or import it as a module).

4. Wrap each wallet-scoped endpoint with @require_wallet:
       Before:
           @app.route('/api/robots', methods=['GET'])
           def list_robots():
               wallet = request.args.get('wallet')
               ...

       After:
           @app.route('/api/robots', methods=['GET'])
           @require_wallet
           def list_robots():
               wallet = g.nwo_wallet     # provided by the decorator, verified
               ...

   IMPORTANT: stop reading wallet from request.args / request.json. The
   verified wallet is in g.nwo_wallet. Reading from args still works while
   NWO_AUTH_REQUIRED=false (lenient mode), but once you flip the flag, the
   verified value is the only safe one to trust.

5. Migration order:
       a. Deploy this code with NWO_AUTH_REQUIRED=false.
          Endpoints accept BOTH old (?wallet=...) and new (signed headers)
          requests. Old clients keep working.
       b. Deploy the new pages.jsx to HF Space. Frontend now sends signed
          headers. Sign in, verify everything works.
       c. Flip NWO_AUTH_REQUIRED=true on Render. Old/unsigned requests now
          rejected with 401. Verify nothing breaks.
       d. Remove any leftover ?wallet= URL handling from app.py.

ROLLBACK
--------
Set NWO_AUTH_REQUIRED=false. The decorator falls back to lenient mode and
accepts unsigned requests (using ?wallet= or body wallet). This restores
prior behavior immediately.

LIMITATIONS / KNOWN GAPS
------------------------
- Session is bound to origin, but a stolen session sig from one user's
  localStorage is still valid for the rest of its 1-hour TTL. XSS in the
  React app would let an attacker grab it. Mitigation: keep the React app
  XSS-clean, ship a strict CSP later.
- No revocation list. Once a session is signed, it's valid until expiry.
  If a user's machine is compromised mid-session, you can't revoke. For now
  the 1-hour TTL is the only mitigation.
- Server clock skew matters. We allow +/- 5 minutes between user clock and
  server clock when validating issued_at/expires.
"""

import os
import re
import time
from functools import wraps
from datetime import datetime

from flask import request, jsonify, g
from eth_account.messages import encode_defunct
from eth_account import Account


# ----- Configuration via env vars -----

def _auth_required():
    return os.environ.get('NWO_AUTH_REQUIRED', 'false').lower() in ('true', '1', 'yes')

def _allowed_origins():
    raw = os.environ.get('NWO_ALLOWED_ORIGIN', 'https://cpater-nwo-capital.hf.space')
    return [s.strip().lower() for s in raw.split(',') if s.strip()]

# Allow up to 5 minutes of clock skew between client and server
CLOCK_SKEW_MS = 5 * 60 * 1000

# Maximum acceptable session lifetime — even if message says expires=24h,
# we cap server-side at 1 hour to limit blast radius if a session leaks.
MAX_SESSION_TTL_MS = 60 * 60 * 1000


# ----- Message parsing -----

# The message is the EXACT format produced by nwoBuildSessionMessage in pages.jsx.
# Lines are separated by \n. We parse the key:value lines.
_KV_LINE = re.compile(r'^(origin|wallet|issued_at|expires|nonce):\s*(.+)$')

def _parse_session_message(message):
    """Parse the signed message into a dict. Returns None if malformed."""
    if not message or not isinstance(message, str):
        return None
    fields = {}
    for line in message.split('\n'):
        m = _KV_LINE.match(line.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    required = {'origin', 'wallet', 'issued_at', 'expires', 'nonce'}
    if not required.issubset(fields.keys()):
        return None
    return fields


def _iso_to_ms(iso):
    """Parse ISO 8601 timestamp into milliseconds since epoch."""
    try:
        # JavaScript .toISOString() always uses 'Z' suffix, e.g. 2026-05-01T12:00:00.000Z
        # Python's fromisoformat doesn't accept 'Z' until 3.11, so handle it manually.
        s = iso.rstrip('Z')
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# ----- Signature verification -----

def _verify_signature(message, signature, claimed_wallet):
    """Recover the signer from the message+signature; compare to claimed_wallet.
    Returns (ok, error_string)."""
    try:
        # personal_sign in MetaMask uses EIP-191 prefix; encode_defunct handles that.
        signable = encode_defunct(text=message)
        recovered = Account.recover_message(signable, signature=signature)
    except Exception as e:
        return False, 'signature decode failed: ' + str(e)
    if recovered.lower() != claimed_wallet.lower():
        return False, 'signature does not match wallet'
    return True, None


def _validate_session(wallet_header, message, signature):
    """Run all checks. Returns (verified_wallet, error_string)."""
    if not wallet_header or not message or not signature:
        return None, 'missing auth headers'

    # The message is base64-encoded over the wire (HTTP headers can't contain
    # newlines). Decode it back to the original multi-line string before
    # parsing/verifying. If decoding fails, fall through and treat as plaintext
    # so legacy clients (none yet) can still work during transition.
    try:
        import base64
        decoded = base64.b64decode(message, validate=False).decode('utf-8')
        # Sanity: a decoded session message must contain "wallet:" line marker
        if 'wallet:' in decoded and 'origin:' in decoded:
            message = decoded
    except Exception:
        pass  # treat as plaintext

    fields = _parse_session_message(message)
    if not fields:
        return None, 'malformed message'

    # Origin must match allowed list (mitigates token reuse from another site)
    msg_origin = fields['origin'].lower()
    allowed = _allowed_origins()
    if msg_origin not in allowed:
        return None, 'origin not allowed'

    # Wallet in header must match wallet in message (and be lowercase)
    msg_wallet = fields['wallet'].lower()
    if msg_wallet != wallet_header.lower():
        return None, 'header/message wallet mismatch'

    # Timestamps
    issued_ms = _iso_to_ms(fields['issued_at'])
    expires_ms = _iso_to_ms(fields['expires'])
    if issued_ms is None or expires_ms is None:
        return None, 'malformed timestamp'

    now_ms = int(time.time() * 1000)
    # Issued in the future (beyond skew) is suspicious
    if issued_ms > now_ms + CLOCK_SKEW_MS:
        return None, 'issued_at in the future'
    # Expired
    if now_ms > expires_ms + CLOCK_SKEW_MS:
        return None, 'session expired'
    # Cap session lifetime regardless of what message claimed
    if expires_ms - issued_ms > MAX_SESSION_TTL_MS + CLOCK_SKEW_MS:
        return None, 'session ttl exceeds maximum'

    # Finally: signature
    ok, err = _verify_signature(message, signature, msg_wallet)
    if not ok:
        return None, err

    return msg_wallet, None


# ----- Flask decorator -----

def require_wallet(fn):
    """Decorator: enforces wallet auth on a Flask view.
    On success, sets g.nwo_wallet to the verified lowercase wallet address.
    On failure: 401 if NWO_AUTH_REQUIRED, otherwise lenient fallback."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        wallet_header = request.headers.get('X-NWO-Wallet') or request.headers.get('x-nwo-wallet')
        message       = request.headers.get('X-NWO-Message') or request.headers.get('x-nwo-message')
        signature     = request.headers.get('X-NWO-Signature') or request.headers.get('x-nwo-signature')

        verified_wallet, err = _validate_session(wallet_header, message, signature)

        if verified_wallet:
            g.nwo_wallet = verified_wallet
            return fn(*args, **kwargs)

        # No valid signature.
        if _auth_required():
            return jsonify({
                'error': 'unauthorized',
                'detail': err or 'missing or invalid signature',
            }), 401

        # Lenient (legacy) fallback: trust the unsigned wallet param.
        # This is INSECURE and only acceptable while NWO_AUTH_REQUIRED=false
        # during the migration window.
        legacy = (
            request.args.get('wallet')
            or (request.is_json and (request.get_json(silent=True) or {}).get('wallet'))
        )
        if legacy:
            g.nwo_wallet = legacy.lower()
            g.nwo_legacy_unsigned = True
            return fn(*args, **kwargs)

        return jsonify({'error': 'unauthorized', 'detail': 'no wallet provided'}), 401

    return wrapper


# ----- Optional: /api/auth/echo for frontend smoke test -----

def register_auth_routes(app):
    """Call once after app = Flask(__name__) to add a debug endpoint.
    GET /api/auth/echo with signed headers returns the verified wallet."""
    @app.route('/api/auth/echo', methods=['GET'])
    @require_wallet
    def auth_echo():
        return jsonify({
            'verified_wallet': g.nwo_wallet,
            'auth_required': _auth_required(),
            'legacy_unsigned': bool(getattr(g, 'nwo_legacy_unsigned', False)),
            'allowed_origins': _allowed_origins(),
        })


# =============================================================================
# EXAMPLE: how to wrap your existing endpoints
# =============================================================================
#
# from auth_patch import require_wallet, register_auth_routes
# from flask import g
#
# app = Flask(__name__)
# register_auth_routes(app)
#
# @app.route('/api/robots', methods=['GET'])
# @require_wallet
# def list_robots():
#     wallet = g.nwo_wallet     # verified, lowercase
#     # ... query DB filtered by wallet ...
#     return jsonify({'robots': [...]})
#
# @app.route('/api/robots', methods=['POST'])
# @require_wallet
# def create_robot():
#     wallet = g.nwo_wallet
#     body = request.get_json() or {}
#     # IMPORTANT: do NOT read 'wallet' from body anymore — only g.nwo_wallet
#     name = body.get('name')
#     # ... insert into DB with owner=wallet ...
#     return jsonify({'id': '...', 'name': name, ...})
#
# @app.route('/api/api-keys', methods=['GET'])
# @require_wallet
# def list_api_keys():
#     wallet = g.nwo_wallet
#     # ... query DB ...
#
# @app.route('/api/api-keys', methods=['POST'])
# @require_wallet
# def create_api_key():
#     wallet = g.nwo_wallet
#     body = request.get_json() or {}
#     name = body.get('name')
#     # ... generate + store hashed key, return key once ...
#
# @app.route('/api/api-keys/<key_id>', methods=['DELETE'])
# @require_wallet
# def delete_api_key(key_id):
#     wallet = g.nwo_wallet
#     # ... delete only if the key's owner == wallet ...
#
# @app.route('/api/chat', methods=['POST'])
# @require_wallet
# def chat():
#     wallet = g.nwo_wallet
#     body = request.get_json() or {}
#     message = body.get('message')
#     robot_id = body.get('robot_id')
#     # ... echo for now ...
#     return jsonify({'response': '[echo] ' + message})
#
# =============================================================================


# =============================================================================
# UNIT TESTS — run this file directly to verify the auth logic
# =============================================================================

if __name__ == '__main__':
    print("Running auth_patch self-tests...")

    # Generate a test signature using a known private key
    test_pk = '0x' + '11' * 32  # private key
    test_acct = Account.from_key(test_pk)
    test_wallet = test_acct.address.lower()

    issued_ms = int(time.time() * 1000)
    expires_ms = issued_ms + 60 * 60 * 1000
    msg = '\n'.join([
        'NWO Capital - session authorization',
        '',
        'Signing this message proves you control this wallet and authorizes',
        'the dapp at the origin below to make API requests on your behalf',
        'until the expiry timestamp. This is NOT a transaction. No fees.',
        '',
        'origin:    https://cpater-nwo-capital.hf.space',
        'wallet:    ' + test_wallet,
        'issued_at: ' + datetime.utcfromtimestamp(issued_ms/1000).isoformat() + 'Z',
        'expires:   ' + datetime.utcfromtimestamp(expires_ms/1000).isoformat() + 'Z',
        'nonce:     deadbeef' + '00' * 12,
    ])

    signable = encode_defunct(text=msg)
    signed = test_acct.sign_message(signable)
    test_sig = signed.signature.hex()

    # Test 1: valid signature passes
    os.environ['NWO_ALLOWED_ORIGIN'] = 'https://cpater-nwo-capital.hf.space'
    verified, err = _validate_session(test_wallet, msg, test_sig)
    assert verified == test_wallet, f"Test 1 failed: {err}"
    print("  OK: valid signature accepted")

    # Test 2: tampered message rejected
    bad_msg = msg.replace('issued_at:', 'issued_at: garbage\n#')
    verified, err = _validate_session(test_wallet, bad_msg, test_sig)
    assert verified is None
    print("  OK: tampered message rejected: " + str(err))

    # Test 3: wrong wallet header rejected
    other_wallet = '0x' + '00' * 20
    verified, err = _validate_session(other_wallet, msg, test_sig)
    assert verified is None
    print("  OK: wrong wallet rejected: " + str(err))

    # Test 4: expired session rejected
    expired_issued = issued_ms - (3 * 60 * 60 * 1000)  # 3h ago
    expired_expires = expired_issued + 60 * 60 * 1000  # expired 2h ago
    expired_msg = msg.replace(
        datetime.utcfromtimestamp(issued_ms/1000).isoformat() + 'Z',
        datetime.utcfromtimestamp(expired_issued/1000).isoformat() + 'Z'
    ).replace(
        datetime.utcfromtimestamp(expires_ms/1000).isoformat() + 'Z',
        datetime.utcfromtimestamp(expired_expires/1000).isoformat() + 'Z'
    )
    expired_signed = test_acct.sign_message(encode_defunct(text=expired_msg))
    verified, err = _validate_session(test_wallet, expired_msg, expired_signed.signature.hex())
    assert verified is None
    print("  OK: expired session rejected: " + str(err))

    # Test 5: wrong origin rejected
    os.environ['NWO_ALLOWED_ORIGIN'] = 'https://example.com'
    verified, err = _validate_session(test_wallet, msg, test_sig)
    assert verified is None
    print("  OK: wrong origin rejected: " + str(err))

    print("\nAll tests passed.")
