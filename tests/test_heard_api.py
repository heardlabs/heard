"""heard-api auth client.

Pins the wire contract with api.heard.dev's /v1/auth/{request,verify}
endpoints, plus the error normalization the onboarding flow depends
on for human-readable messages."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from heard import heard_api


class _FakeResp:
    def __init__(self, body: dict, status: int = 200):
        self._body = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, response_or_exception):
    calls: list[urllib.request.Request] = []

    def _fake(req, *a, **kw):
        calls.append(req)
        if isinstance(response_or_exception, BaseException):
            raise response_or_exception
        return response_or_exception

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body).encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://api.heard.dev/v1/auth/x",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=fp,
    )


# --- request_code --------------------------------------------------------


def test_request_code_succeeds_on_ok_response(monkeypatch):
    calls = _patch_urlopen(monkeypatch, _FakeResp({"ok": True, "expires_in_s": 600}))
    heard_api.request_code("user@example.com", base_url="https://api.test.dev")

    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://api.test.dev/v1/auth/request"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"email": "user@example.com"}


def test_request_code_raises_on_invalid_email(monkeypatch):
    _patch_urlopen(monkeypatch, _http_error(400, {"error": "invalid_email"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.request_code("not-an-email")
    assert exc.value.status == 400
    assert exc.value.reason == "invalid_email"


def test_request_code_raises_on_email_send_failure(monkeypatch):
    _patch_urlopen(monkeypatch, _http_error(502, {"error": "email_send_failed"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.request_code("ok@example.com")
    assert exc.value.status == 502
    assert exc.value.reason == "email_send_failed"


def test_request_code_raises_on_network_failure(monkeypatch):
    _patch_urlopen(monkeypatch, urllib.error.URLError("dns lookup failed"))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.request_code("ok@example.com")
    assert exc.value.status == 0
    assert exc.value.reason == "network_unreachable"


def test_request_code_raises_on_unexpected_payload(monkeypatch):
    """Proxy returned 200 but body shape isn't what we expected — still
    treat as failure rather than silently accepting."""
    _patch_urlopen(monkeypatch, _FakeResp({"weird": "shape"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.request_code("ok@example.com")
    assert exc.value.reason == "unexpected_response"


# --- verify_code ---------------------------------------------------------


def test_verify_code_returns_token_info_on_success(monkeypatch):
    payload = {
        "token": "tok_abc123",
        "plan": "trial",
        "trial_expires_at": 1780000000000,
        "email": "user@example.com",
        "returning": False,
    }
    calls = _patch_urlopen(monkeypatch, _FakeResp(payload))
    info = heard_api.verify_code(
        "user@example.com", "123456", base_url="https://api.test.dev"
    )

    assert info.token == "tok_abc123"
    assert info.plan == "trial"
    assert info.email == "user@example.com"
    assert info.trial_expires_at == 1780000000000
    assert info.returning is False

    body = json.loads(calls[0].data.decode("utf-8"))
    assert body == {"email": "user@example.com", "code": "123456"}


def test_verify_code_returning_user_flag(monkeypatch):
    """Reinstall / new-Mac scenario: same email, existing token gets
    handed back. Onboarding can show 'Welcome back' instead of
    'Trial started'."""
    _patch_urlopen(
        monkeypatch,
        _FakeResp(
            {
                "token": "tok_existing",
                "plan": "pro",
                "trial_expires_at": 0,
                "email": "user@example.com",
                "returning": True,
            }
        ),
    )
    info = heard_api.verify_code("user@example.com", "123456")
    assert info.returning is True
    assert info.plan == "pro"


def test_verify_code_wrong_code(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        _http_error(401, {"error": "wrong_code", "attempts_remaining": 4}),
    )
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.verify_code("user@example.com", "000000")
    assert exc.value.status == 401
    assert exc.value.reason == "wrong_code"


def test_verify_code_expired(monkeypatch):
    _patch_urlopen(monkeypatch, _http_error(401, {"error": "code_expired"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.verify_code("user@example.com", "123456")
    assert exc.value.reason == "code_expired"


def test_verify_code_too_many_attempts(monkeypatch):
    _patch_urlopen(monkeypatch, _http_error(429, {"error": "too_many_attempts"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.verify_code("user@example.com", "123456")
    assert exc.value.status == 429
    assert exc.value.reason == "too_many_attempts"


def test_verify_code_missing_token_in_response(monkeypatch):
    """Defensive: 200 with no token field → don't silently mint an
    empty-token entry, raise instead."""
    _patch_urlopen(monkeypatch, _FakeResp({"plan": "trial"}))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.verify_code("user@example.com", "123456")
    assert exc.value.reason == "missing_token"


def test_verify_code_network_unreachable(monkeypatch):
    _patch_urlopen(monkeypatch, urllib.error.URLError("offline"))
    with pytest.raises(heard_api.HeardApiError) as exc:
        heard_api.verify_code("user@example.com", "123456")
    assert exc.value.status == 0
    assert exc.value.reason == "network_unreachable"
