"""Auth v2 (CONTRACTS section 9): demo/jwt modes, POST /v1/auth/token, HS256 JWT."""
from __future__ import annotations

import base64
import json

from wherugo_backend import auth as auth_mod

from helpers import AUTH


def _get_token(client, api_key="demo-api-key"):
    return client.post("/v1/auth/token", json={"api_key": api_key})


# --- token endpoint ------------------------------------------------------------


def test_token_endpoint_issues_jwt_for_demo_key(client):
    resp = _get_token(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in"] == 86400
    token = body["token"]
    assert token.count(".") == 2
    claims = auth_mod.jwt_decode(token, auth_mod.jwt_secret())
    assert claims["tenant"] == "t_demo"
    assert isinstance(claims["exp"], int)


def test_token_endpoint_wrong_key_401(client):
    resp = _get_token(client, api_key="yanlis-anahtar")
    assert resp.status_code == 401
    assert "detail" in resp.json()
    assert _get_token(client, api_key="").status_code == 401


# --- demo mode (default): Bearer demo AND a valid JWT both work -----------------


def test_demo_mode_accepts_demo_and_jwt(client, monkeypatch):
    monkeypatch.delenv("WHERUGO_AUTH_MODE", raising=False)
    assert client.get("/v1/stores", headers=AUTH).status_code == 200
    token = _get_token(client).json()["token"]
    resp = client.get("/v1/stores", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()[0]["tenant_id"] == "t_demo"


# --- jwt mode: Bearer demo is rejected, JWT still works -------------------------


def test_jwt_mode_rejects_demo_but_accepts_jwt(client, monkeypatch):
    monkeypatch.setenv("WHERUGO_AUTH_MODE", "jwt")
    resp = client.get("/v1/stores", headers=AUTH)
    assert resp.status_code == 401
    assert "detail" in resp.json()

    token = _get_token(client).json()["token"]  # token endpoint needs no auth
    assert client.get("/v1/stores",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


# --- JWT validation: expiry + tampering ------------------------------------------


def test_expired_jwt_rejected(client):
    expired = auth_mod.issue_token("t_demo", ttl_sec=-10)
    resp = client.get("/v1/stores", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


def test_tampered_jwt_rejected(client):
    token = auth_mod.issue_token("t_demo")
    header_b64, payload_b64, sig_b64 = token.split(".")
    forged_claims = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    forged_claims["tenant"] = "t_other"
    forged_b64 = base64.urlsafe_b64encode(
        json.dumps(forged_claims).encode()).rstrip(b"=").decode()
    forged = f"{header_b64}.{forged_b64}.{sig_b64}"
    resp = client.get("/v1/stores", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_malformed_tokens_rejected(client):
    for bad in ("abc", "a.b", "a.b.c", "..", ""):
        resp = client.get("/v1/stores", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401, bad


def test_jwt_without_exp_rejected(client):
    token = auth_mod.jwt_encode({"tenant": "t_demo"}, auth_mod.jwt_secret())
    assert client.get("/v1/stores",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401
