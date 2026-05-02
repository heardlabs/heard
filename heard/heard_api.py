"""Client for api.heard.dev's auth endpoints.

Used by the onboarding flow (CLI + menu bar) to mint a Heard token
without the user ever seeing our ElevenLabs key. Mirrors the wire
contract pinned in heard-api/src/signup.ts:

  POST /v1/auth/request  { email }
    → { ok: true, expires_in_s }
    → email lands in the user's inbox with a 6-digit code

  POST /v1/auth/verify   { email, code }
    → { token, plan, trial_expires_at, email, returning }

Errors are normalized to ``HeardApiError`` with ``status`` + ``reason``
so the caller can show useful messages without re-implementing the
HTTP error parsing logic for every endpoint.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

try:
    import certifi  # type: ignore
except ImportError:  # pragma: no cover
    certifi = None  # type: ignore

DEFAULT_BASE_URL = "https://api.heard.dev"
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class TokenInfo:
    token: str
    plan: str
    email: str
    trial_expires_at: int
    returning: bool


class HeardApiError(RuntimeError):
    """Auth call failed. ``status`` is the HTTP status (0 for network);
    ``reason`` is the proxy's machine-readable error string when one
    was returned (e.g. ``invalid_email``, ``wrong_code``,
    ``code_expired``, ``too_many_attempts``)."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(f"heard-api {status} {reason}: {detail}".strip())
        self.status = status
        self.reason = reason
        self.detail = detail


def _ssl_ctx() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def _post_json(
    url: str, body: dict, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_ctx()) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        reason = ""
        detail = ""
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
            reason = (payload.get("error") or "").strip()
            detail = json.dumps(payload)[:300]
        except Exception:
            detail = str(e)
        raise HeardApiError(e.code, reason or "http_error", detail) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise HeardApiError(0, "network_unreachable", str(e)) from e


def request_code(email: str, base_url: str = DEFAULT_BASE_URL) -> None:
    """Trigger a 6-digit code email. Raises ``HeardApiError`` on
    invalid email, send failure, or network issue."""
    payload = _post_json(f"{base_url.rstrip('/')}/v1/auth/request", {"email": email})
    if not payload.get("ok"):
        raise HeardApiError(500, "unexpected_response", json.dumps(payload)[:200])


def verify_code(
    email: str, code: str, base_url: str = DEFAULT_BASE_URL
) -> TokenInfo:
    """Exchange a 6-digit code for a Heard token. Raises
    ``HeardApiError(401, 'wrong_code')`` on bad code,
    ``HeardApiError(401, 'code_expired')`` on expiry, etc."""
    payload = _post_json(
        f"{base_url.rstrip('/')}/v1/auth/verify", {"email": email, "code": code}
    )
    token = (payload.get("token") or "").strip()
    if not token:
        raise HeardApiError(500, "missing_token", json.dumps(payload)[:200])
    return TokenInfo(
        token=token,
        plan=str(payload.get("plan") or "trial"),
        email=str(payload.get("email") or email),
        trial_expires_at=int(payload.get("trial_expires_at") or 0),
        returning=bool(payload.get("returning", False)),
    )
