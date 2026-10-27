"""HTTP API hardening: authentication, repo path confinement, CORS.

No database or LLM: the handlers' downstream calls are monkeypatched, so each
test exercises only the guard in front of them.
"""

import importlib
import os
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import auth
import impact
import indexer

ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/explorer/abc123"
TOKEN = "t" * 40  


def _client(monkeypatch, **env):
    """A fresh app built under `env` — CORS and the config check run at import."""
    for k in ("AUTH_MODE", "API_TOKEN", "ALB_ARN", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DAILY_TOKEN_QUOTA", "0")  # quota has its own tests (Neo4j-backed)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main

    return TestClient(importlib.reload(main).app)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    (root / "shop").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", root / "escape")  # in the root, points out of it
    monkeypatch.setenv("REPOS_ROOT", str(root))
    calls = []
    monkeypatch.setattr(indexer, "index_repo",
                        lambda path, repo="": calls.append((path, repo)) or {"ok": True})
    return root, calls


# --- path confinement -------------------------------------------------------


@pytest.mark.parametrize("name", ["../secret", "/etc", "shop/../..", "a/b", "..", ".",
                                  "-rf", ""])
def test_index_rejects_anything_but_a_plain_name(monkeypatch, repos, name):
    _, calls = repos
    r = _client(monkeypatch).post("/index", json={"repo": name})
    assert r.status_code == 400 and calls == []


def test_index_rejects_symlink_escaping_the_root(monkeypatch, repos):
    _, calls = repos
    r = _client(monkeypatch).post("/index", json={"repo": "escape"})
    assert r.status_code == 400 and calls == []


def test_index_resolves_a_name_under_the_root(monkeypatch, repos):
    root, calls = repos
    c = _client(monkeypatch)
    assert c.post("/index", json={"repo": "shop"}).status_code == 200
    assert calls == [(str(root / "shop"), "shop")]
    r = c.post("/index", json={"repo": "missing"})
    assert r.status_code == 404 and str(root) not in r.text  # never echo the path


def test_index_no_longer_accepts_a_path(monkeypatch, repos):
    r = _client(monkeypatch).post("/index", json={"path": "/etc"})
    assert r.status_code == 422


def test_impact_is_confined_too(monkeypatch, repos):
    seen = []
    monkeypatch.setattr(impact, "analyze", lambda *a, **k: seen.append(a) or 1 / 0)
    r = _client(monkeypatch).post("/impact", json={"repo": "../secret"})
    assert r.status_code == 400 and seen == []


def test_revision_cannot_become_a_git_option():
    for rev in ("--output=/tmp/x", "-n", "", "HEAD\nx"):
        with pytest.raises(ValueError):
            impact._resolve("/nonexistent", rev)


# --- authentication: token --------------------------------------------------


def test_token_mode(monkeypatch, repos):
    c = _client(monkeypatch, AUTH_MODE="token", API_TOKEN=TOKEN)
    assert c.get("/health").status_code == 200  # ALB health checks are unauthenticated
    assert c.get("/usage").status_code == 401
    assert c.get("/usage", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/usage", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert c.get("/usage", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.post("/index", json={"repo": "shop"}).status_code == 401


@pytest.mark.parametrize("env", [
    {"AUTH_MODE": "bogus"},
    {"AUTH_MODE": "token", "API_TOKEN": "short"},
    {"AUTH_MODE": "alb"},
    {"AUTH_MODE": "alb", "ALB_ARN": "not-an-arn"},
    {"CORS_ORIGINS": "*"},
])
def test_misconfiguration_fails_at_startup(monkeypatch, env):
    with pytest.raises(RuntimeError):
        _client(monkeypatch, **env)


# --- authentication: ALB ----------------------------------------------------


@pytest.fixture
def alb_key(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    fetched = []

    def fake_fetch(kid, region):
        fetched.append((kid, region))
        return pem

    monkeypatch.setattr(auth, "_alb_public_key", fake_fetch)
    return key, fetched


def _alb_token(key, *, signer=ARN, exp_in=60, kid="key-1"):
    return jwt.encode({"sub": "u1", "email": "a@b.c", "exp": int(time.time()) + exp_in},
                      key, algorithm="ES256", headers={"kid": kid, "signer": signer})


def test_alb_mode_accepts_only_tokens_signed_for_our_alb(monkeypatch, repos, alb_key):
    key, fetched = alb_key
    c = _client(monkeypatch, AUTH_MODE="alb", ALB_ARN=ARN)

    def get(token):
        return c.get("/usage", headers={"x-amzn-oidc-data": token}).status_code

    assert c.get("/usage").status_code == 401
    assert get(_alb_token(key)) == 200
    assert fetched[0] == ("key-1", "us-east-1")  # region taken from the ARN
    # a genuine AWS signature from someone ELSE's load balancer
    assert get(_alb_token(key, signer=ARN.replace("abc123", "evil"))) == 401
    assert get(_alb_token(key, exp_in=-10)) == 401  # expired
    assert get(_alb_token(ec.generate_private_key(ec.SECP256R1()))) == 401  # forged
    assert get(_alb_token(key, kid="../../x")) == 401  # kid lands in a URL
    assert get("not-a-jwt") == 401


# --- CORS -------------------------------------------------------------------


def test_cors_is_off_by_default(monkeypatch, repos):
    r = _client(monkeypatch).get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_only_listed_origins(monkeypatch, repos):
    c = _client(monkeypatch, CORS_ORIGINS="https://explorer.example.com")
    ok = c.get("/health", headers={"Origin": "https://explorer.example.com"})
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://explorer.example.com"
    assert "access-control-allow-origin" not in bad.headers
