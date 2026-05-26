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
import re
import ssl
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    # True iff this token was minted by /v1/auth/anonymous (anon-trial
    # flow). Lets the wizard / menu show "Sign in to extend your trial"
    # instead of acting like the user has a verified account. Defaults
    # to False so existing callers (verify_code, claim_install_code)
    # don't need to touch this field.
    is_anonymous: bool = False


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


def _request_json(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Generic JSON request helper. Used by _post_json (POST), _get_json
    (GET), and 3B's DELETE /v1/devices/:id. Same HTTPError → HeardApiError
    mapping so callers get a uniform error model."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "User-Agent": "Heard-cli/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_ctx()) as resp:
            text = resp.read().decode("utf-8")
        return json.loads(text) if text else {}
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


def _post_json(
    url: str, body: dict, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict:
    return _request_json("POST", url, body=body, timeout_s=timeout_s)


def _get_json(
    url: str, token: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict:
    return _request_json("GET", url, token=token, timeout_s=timeout_s)


def request_code(email: str, base_url: str = DEFAULT_BASE_URL) -> None:
    """Trigger a 6-digit code email. Raises ``HeardApiError`` on
    invalid email, send failure, or network issue."""
    payload = _post_json(f"{base_url.rstrip('/')}/v1/auth/request", {"email": email})
    if not payload.get("ok"):
        raise HeardApiError(500, "unexpected_response", json.dumps(payload)[:200])


# ---------------------------------------------------------------------
# Anonymous trial (device-bound, 7-day, 5K chars/day)
# ---------------------------------------------------------------------
#
# The desktop app's first-launch path: mint a Heard token without
# asking the user for an email. Server enforces per-device idempotency
# via accounts.device_id (unique-when-not-null), so re-launching with
# the same device.id always returns the same trial — re-running the
# endpoint isn't a way to multiply the cap. Sign-in via the normal
# verify_code / claim_install_code paths sends the same device_id as
# prior_device_id so the server can delete the anon row.

def _device_id_path(data_dir: Path) -> Path:
    """Where the device's anon-trial identity lives. One file under the
    daemon's state dir. Lives next to daemon.sock / daemon.log because
    it's daemon-runtime state — `rm -rf` the state dir and the user
    gets a fresh device, exactly the abuse-bounded contract the server
    expects (and the partial-unique device_id index enforces)."""
    return data_dir / "device.id"


def load_or_create_device_id(data_dir: Path) -> str:
    """Return a stable per-machine device id, creating it (UUID4) on
    first call. Idempotent: subsequent calls return the same value.
    Safe to call from multiple threads — the file write is small
    enough to be atomic on macOS APFS.

    The id isn't tied to hardware (no IOPlatformUUID, no MAC), so a
    determined user can rotate by deleting `device.id`. That's by
    design — the abuse mitigation is the (tight cap × short trial)
    product, not unforgeable identity. If we later want stronger
    binding, mirror this value into the macOS Keychain so a state-dir
    wipe doesn't reset it (deferred; see PRD).
    """
    p = _device_id_path(data_dir)
    try:
        existing = p.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        # Corrupt / unreadable file — overwrite with a fresh one rather
        # than crashing the boot path.
        pass
    new_id = str(uuid.uuid4())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_id, encoding="utf-8")
    return new_id


def request_anon_trial(
    device_id: str, base_url: str = DEFAULT_BASE_URL
) -> TokenInfo:
    """Mint an anonymous trial token bound to ``device_id``. Same
    device_id always returns the same trial (idempotent on the server
    side). Returns a TokenInfo with is_anonymous=True.

    Raises ``HeardApiError(400, 'invalid_request')`` on a malformed
    device_id, ``HeardApiError(402, 'trial_expired')`` when the device
    has already burned its 7-day anon trial (must sign in to extend),
    ``HeardApiError(0, 'network_unreachable')`` on network failure —
    callers should treat the network case as a retry candidate, not a
    hard failure (see daemon boot path)."""
    payload = _post_json(
        f"{base_url.rstrip('/')}/v1/auth/anonymous",
        {"device_id": device_id},
    )
    token = (payload.get("token") or "").strip()
    if not token:
        raise HeardApiError(500, "missing_token", json.dumps(payload)[:200])
    return TokenInfo(
        token=token,
        plan=str(payload.get("plan") or "trial"),
        email="",  # anon accounts have a synthetic placeholder; never surface
        trial_expires_at=int(payload.get("trial_expires_at") or 0),
        returning=False,
        is_anonymous=bool(payload.get("is_anonymous", True)),
    )


def _local_device_name() -> str:
    """Best-effort hostname for the new device_session row (3A). Uses
    socket.gethostname() — typically "<user>'s MacBook Pro" or similar
    on macOS. Falls back to "Mac" if anything goes wrong, so the
    Connected Macs panel always has something readable."""
    try:
        import socket

        name = (socket.gethostname() or "").strip()
        # Strip the trailing ".local" macOS appends so the dashboard
        # shows "Christian's MacBook Pro" not the FQDN-ish form.
        if name.lower().endswith(".local"):
            name = name[: -len(".local")]
        return name or "Mac"
    except Exception:
        return "Mac"


def verify_code(
    email: str,
    code: str,
    base_url: str = DEFAULT_BASE_URL,
    prior_device_id: str | None = None,
) -> TokenInfo:
    """Exchange a 6-digit code for a Heard token. Raises
    ``HeardApiError(401, 'wrong_code')`` on bad code,
    ``HeardApiError(401, 'code_expired')`` on expiry, etc.

    ``prior_device_id``: if provided, signals the server to delete any
    anonymous trial account this device was running before creating
    the verified account. No-op if the device never ran an anon trial."""
    body = {"email": email, "code": code, "device_name": _local_device_name()}
    if prior_device_id:
        body["prior_device_id"] = prior_device_id
    payload = _post_json(
        f"{base_url.rstrip('/')}/v1/auth/verify",
        body,
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


# 32-letter ambiguity-free alphabet (no 0/1/I/O). Mirror of
# INSTALL_CODE_ALPHABET in heard-api/src/db.ts. We accept the dashed or
# undashed form, lowercased or uppercased, and canonicalize before
# sending so the server's hash matches.
_INSTALL_CODE_RE = re.compile(r"[^A-HJ-NP-Z2-9]")


def claim_install_code(
    code: str,
    base_url: str = DEFAULT_BASE_URL,
    prior_device_id: str | None = None,
) -> TokenInfo:
    """Exchange a single-use install code (minted by heard.dev's
    /signin web flow) for a fresh Heard bearer + plan info. 3A: the
    server now INSERTS a new device_session per claim — other Macs
    already signed in keep their own sessions (no silent kick).
    Other devices can be revoked from the dashboard's Connected Macs
    panel.

    ``prior_device_id``: if provided, signals the server to delete any
    anonymous trial account this device was running before creating
    the verified session. The OAuth bridge happens on the web (no
    device id known there), so claim time is the canonical merge
    moment for OAuth users.

    Raises ``HeardApiError(400, 'invalid_request')`` on shape failures,
    ``HeardApiError(410, 'code_expired'|'code_expired_or_unknown')`` on
    expiry / unknown codes, ``HeardApiError(410, 'account_missing')``
    when the bound account was deleted between mint and claim."""
    canonical = _INSTALL_CODE_RE.sub("", (code or "").upper())
    if len(canonical) != 8:
        raise HeardApiError(
            400, "invalid_request", "code must canonicalize to 8 chars"
        )
    body = {"code": canonical, "device_name": _local_device_name()}
    if prior_device_id:
        body["prior_device_id"] = prior_device_id
    payload = _post_json(
        f"{base_url.rstrip('/')}/v1/auth/claim",
        body,
    )
    token = (payload.get("token") or "").strip()
    if not token:
        raise HeardApiError(500, "missing_token", json.dumps(payload)[:200])
    return TokenInfo(
        token=token,
        plan=str(payload.get("plan") or "trial"),
        email=str(payload.get("email") or ""),
        trial_expires_at=int(payload.get("trial_expires_at") or 0),
        returning=False,
    )


# 3B device list / revoke. Used by Settings → Account to render the
# Connected Macs panel + revoke individual sessions. Same Bearer-auth
# shape as the synth path.

@dataclass
class DeviceInfo:
    id: str
    device_name: str | None
    device_kind: str
    user_agent: str | None
    created_at: int     # epoch ms
    last_seen_at: int   # epoch ms


def list_devices(
    token: str, base_url: str = DEFAULT_BASE_URL
) -> tuple[list[DeviceInfo], str | None]:
    """GET /v1/devices for the bearer's account. Returns
    (devices, current_session_id) — current_session_id is the row tied
    to this bearer so the UI can render a "This Mac" marker."""
    payload = _get_json(f"{base_url.rstrip('/')}/v1/devices", token=token)
    rows = payload.get("devices") or []
    devices = [
        DeviceInfo(
            id=str(r.get("id") or ""),
            device_name=(r.get("device_name") or None),
            device_kind=str(r.get("device_kind") or "desktop"),
            user_agent=(r.get("user_agent") or None),
            created_at=int(r.get("created_at") or 0),
            last_seen_at=int(r.get("last_seen_at") or 0),
        )
        for r in rows
        if isinstance(r, dict) and r.get("id")
    ]
    current = payload.get("current_session_id") or None
    return devices, (str(current) if current else None)


def revoke_device(
    token: str, session_id: str, base_url: str = DEFAULT_BASE_URL
) -> None:
    """DELETE /v1/devices/:id. Raises HeardApiError on 404 / network
    failure. Revoking the current session is allowed (server doesn't
    block it) — the daemon's next /v1/synth will 401 with
    `device_revoked` and the existing sign-out flow runs."""
    _request_json(
        "DELETE",
        f"{base_url.rstrip('/')}/v1/devices/{session_id}",
        token=token,
    )
