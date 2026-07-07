"""Auth: demo bearer token -> tenant context. Structured so a real JWT decoder can slot in."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

DEMO_TOKEN = "demo"
DEMO_TENANT_ID = "t_demo"


@dataclass
class AuthContext:
    tenant_id: str
    subject: str = "demo"
    claims: dict | None = None


def decode_token(token: str) -> AuthContext:
    """Resolve a bearer token to an AuthContext.

    Demo mode: the literal token 'demo' maps to tenant t_demo.
    Extension point: parse/verify a JWT here and read its 'tenant' claim, e.g.
        claims = jwt.decode(token, key, algorithms=["RS256"])
        return AuthContext(tenant_id=claims["tenant"], subject=claims.get("sub", ""), claims=claims)
    """
    if token == DEMO_TOKEN:
        return AuthContext(tenant_id=DEMO_TENANT_ID, subject="demo")
    raise HTTPException(status_code=401, detail="invalid token")


def get_auth(authorization: str | None = Header(default=None)) -> AuthContext:
    """FastAPI dependency: require `Authorization: Bearer <token>`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(authorization.split(" ", 1)[1].strip())
