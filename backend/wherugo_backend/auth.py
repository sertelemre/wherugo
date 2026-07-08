"""Auth v1 (demo bearer) + v2 (HS256 JWT, CONTRACTS section 9).

Modes (env WHERUGO_AUTH_MODE, read per-request so it can be flipped in tests):
  demo (default): the literal token 'demo' maps to tenant t_demo (v1 behavior);
                  a valid JWT is ALSO accepted.
  jwt:            only a valid JWT is accepted; 'Bearer demo' is rejected.

JWT: HS256 with stdlib hmac/hashlib/base64 (no external dependency), secret
env WHERUGO_JWT_SECRET, claims {"tenant": str, "exp": int}. Tokens are issued
by POST /v1/auth/token against tenant.api_key_hash (sha256 hex of the API key).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException

DEMO_TOKEN = "demo"
DEMO_TENANT_ID = "t_demo"

AUTH_MODE_ENV = "WHERUGO_AUTH_MODE"
JWT_SECRET_ENV = "WHERUGO_JWT_SECRET"
DEMO_API_KEY_ENV = "WHERUGO_DEMO_API_KEY"
DEFAULT_DEMO_API_KEY = "demo-api-key"
# Fallback so the out-of-the-box demo can issue/verify tokens without config;
# any real deployment must set WHERUGO_JWT_SECRET.
DEFAULT_JWT_SECRET = "wherugo-dev-secret-change-me"
TOKEN_TTL_SEC = 86400


@dataclass
class AuthContext:
    tenant_id: str
    subject: str = "demo"
    claims: dict | None = None


def auth_mode() -> str:
    mode = (os.environ.get(AUTH_MODE_ENV) or "demo").strip().lower()
    return mode if mode in ("demo", "jwt") else "demo"


def jwt_secret() -> str:
    return os.environ.get(JWT_SECRET_ENV) or DEFAULT_JWT_SECRET


def demo_api_key() -> str:
    return os.environ.get(DEMO_API_KEY_ENV) or DEFAULT_DEMO_API_KEY


def hash_api_key(api_key: str) -> str:
    """sha256 hex of an API key — the only form ever stored (tenant.api_key_hash)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# --- minimal HS256 JWT (stdlib only) ------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def jwt_encode(claims: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    p = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret.encode("utf-8"), f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def jwt_decode(token: str, secret: str, *, now: float | None = None) -> dict:
    """Verify + decode an HS256 JWT. Raises ValueError on ANY problem
    (malformed, wrong alg, bad signature, missing/expired exp)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed token")
    h_b64, p_b64, s_b64 = parts
    try:
        header = json.loads(_b64url_decode(h_b64))
        payload = json.loads(_b64url_decode(p_b64))
        sig = _b64url_decode(s_b64)
    except Exception as exc:
        raise ValueError("malformed token") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise ValueError("unsupported algorithm")
    if not isinstance(payload, dict):
        raise ValueError("malformed claims")
    expected = hmac.new(secret.encode("utf-8"), f"{h_b64}.{p_b64}".encode("ascii"),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise ValueError("missing exp claim")
    if (now if now is not None else time.time()) >= float(exp):
        raise ValueError("token expired")
    return payload


def issue_token(tenant_id: str, *, ttl_sec: int = TOKEN_TTL_SEC,
                now: float | None = None) -> str:
    """Issue an HS256 JWT with the CONTRACTS section 9 claims {tenant, exp}."""
    issued = now if now is not None else time.time()
    return jwt_encode({"tenant": tenant_id, "exp": int(issued) + int(ttl_sec)},
                      jwt_secret())


# --- FastAPI dependency ---------------------------------------------------------

def decode_token(token: str) -> AuthContext:
    """Resolve a bearer token to an AuthContext.

    demo mode: literal 'demo' -> t_demo; a valid JWT is also accepted.
    jwt mode:  only a valid JWT; 'demo' is rejected (401)."""
    if auth_mode() == "demo" and token == DEMO_TOKEN:
        return AuthContext(tenant_id=DEMO_TENANT_ID, subject="demo")
    try:
        claims = jwt_decode(token, jwt_secret())
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid token")
    tenant = claims.get("tenant")
    if not tenant or not isinstance(tenant, str):
        raise HTTPException(status_code=401, detail="invalid token: missing tenant claim")
    return AuthContext(tenant_id=tenant, subject=str(claims.get("sub", tenant)), claims=claims)


def get_auth(authorization: str | None = Header(default=None)) -> AuthContext:
    """FastAPI dependency: require `Authorization: Bearer <token>`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(authorization.split(" ", 1)[1].strip())
