"""Request authentication for the HTTP API.

`AUTH_MODE` selects how a request proves who it is:

  alb    Production on AWS. An Application Load Balancer authenticates the user
         (Cognito / OIDC) and forwards a signed JWT in `x-amzn-oidc-data`. We
         VERIFY that signature — trusting the ALB alone would leave the API
         open to anyone who reaches the container directly, and the header is
         trivially forgeable without the check.
  token  `Authorization: Bearer $API_TOKEN`. For scripts, CI and webhooks.
  none   Local development only. Logged loudly at startup.

`/health` is always open: ALB health checks bypass authentication.

The only outbound call here is fetching the ALB's PUBLIC signing key from
AWS, by key id. No request content, and no source code, is ever sent.
"""

from __future__ import annotations

import hmac
import os
import re
import sys
import threading
import time
import urllib.request
from collections import deque
from typing import Any

import jwt
from fastapi import HTTPException, Request

_MODES = {"none", "token", "alb"}
_OPEN_PATHS = {"/health"}
_KID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_ARN = re.compile(r"^arn:aws[\w-]*:elasticloadbalancing:([a-z0-9-]+):\d{12}:loadbalancer/.+$")
_keys: dict[str, str] = {}  # kid -> PEM; ALB keys are immutable per kid


def mode() -> str:
    return os.environ.get("AUTH_MODE", "none").strip().lower()


def check_config() -> None:
    """Fail at startup on a misconfiguration, rather than on the first request —
    or worse, by quietly serving everything unauthenticated."""
    m = mode()
    if m not in _MODES:
        raise RuntimeError(f"AUTH_MODE must be one of {sorted(_MODES)}, got {m!r}")
    if m == "token" and len(os.environ.get("API_TOKEN", "")) < 32:
        raise RuntimeError("AUTH_MODE=token requires API_TOKEN of at least 32 characters")
    if m == "alb" and not _ARN.match(os.environ.get("ALB_ARN", "")):
        raise RuntimeError("AUTH_MODE=alb requires ALB_ARN, the load balancer's full ARN")
    if m == "none":
        print("[auth] WARNING: AUTH_MODE=none — every route is unauthenticated. "
              "Local development only.", file=sys.stderr, flush=True)


def _alb_public_key(kid: str, region: str) -> str:
    if kid not in _keys:
        url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — fixed https host
            _keys[kid] = resp.read().decode()
    return _keys[kid]


def verify_alb(token: str) -> dict[str, Any]:
    """Verify an `x-amzn-oidc-data` JWT and return its claims.

    The `signer` check is what makes this safe: anyone can create their own
    ALB and get validly signed tokens from AWS. Only tokens signed for OUR
    load balancer are accepted.
    """
    arn = os.environ.get("ALB_ARN", "")
    match = _ARN.match(arn)
    if not match:
        raise PermissionError("ALB_ARN is not configured")
    header = jwt.get_unverified_header(token)
    if header.get("signer") != arn:
        raise PermissionError("token was not issued by this load balancer")
    kid = str(header.get("kid", ""))
    if not _KID.match(kid):
        raise PermissionError("malformed key id")
    key = _alb_public_key(kid, match.group(1))
    return jwt.decode(token, key, algorithms=["ES256"], options={"require": ["exp"]})


def require_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency applied to every route. Returns the caller's claims,
    also kept on `request.state.user` for the rate limiter."""
    claims = _authenticate(request)
    request.state.user = claims
    return claims


def _authenticate(request: Request) -> dict[str, Any]:
    if request.url.path in _OPEN_PATHS:
        return {}
    m = mode()
    if m == "none":
        return {"sub": "local"}
    if m == "token":
        expected = os.environ.get("API_TOKEN", "")
        given = request.headers.get("authorization", "")
        scheme, _, value = given.partition(" ")
        if expected and scheme.lower() == "bearer" and hmac.compare_digest(value, expected):
            return {"sub": "token"}
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    if m == "alb":
        token = request.headers.get("x-amzn-oidc-data", "")
        if not token:
            raise HTTPException(status_code=401, detail="not authenticated")
        try:
            return verify_alb(token)
        except Exception as exc:  # noqa: BLE001 — any verification failure is a 401
            raise HTTPException(status_code=401, detail="invalid authentication token") from exc
    raise HTTPException(status_code=500, detail="authentication is misconfigured")


# --- Rate limiting ------------------------------------------------------------
# Every call to an LLM-backed route is paid for by the deployment, and Best-of-N
# attacks work by retrying variations until one gets through. Limit each user to
# LLM_RATE_PER_MIN calls per rolling minute. In-process: with several api
# processes each enforces its own window, so put a limit at the load balancer
# too when you scale out.
_WINDOW = 60.0
_calls: dict[str, deque[float]] = {}
_lock = threading.Lock()


def _rate() -> int:
    return int(os.environ.get("LLM_RATE_PER_MIN", "20"))


def user_key(request: Request) -> str:
    """Who to charge: the authenticated subject, else the client address."""
    user = getattr(request.state, "user", None) or {}
    return str(user.get("sub") or (request.client.host if request.client else "unknown"))


def rate_limit(request: Request) -> None:
    """FastAPI dependency for routes that call the LLM."""
    limit = _rate()
    if limit <= 0:
        return
    key = user_key(request)
    now = time.monotonic()
    with _lock:
        q = _calls.setdefault(key, deque())
        while q and now - q[0] > _WINDOW:
            q.popleft()
        if len(q) >= limit:
            retry = int(_WINDOW - (now - q[0])) + 1
            raise HTTPException(status_code=429, detail="rate limit exceeded",
                                headers={"Retry-After": str(retry)})
        q.append(now)
        if len(_calls) > 10_000:  # drop idle users so the table cannot grow forever
            for k in [k for k, v in _calls.items() if not v or now - v[-1] > _WINDOW]:
                del _calls[k]
