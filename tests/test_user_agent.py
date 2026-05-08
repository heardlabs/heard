"""Smoke tests for the User-Agent header on Heard's outbound HTTP calls.

Cloudflare's bot-fight rule on api.heard.dev rejects the default
``Python-urllib/3.X`` UA with a 403, which manifests in the daemon as
"managed 403 unknown" and a "cloud voices error" indicator in the menu
bar. Every HTTP client we ship to api.heard.dev must therefore set a
real product UA. These tests pin that contract so a regression — a new
client added without a UA, or a UA accidentally renamed back to the
default — fails the build.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _captured_request(stack):
    """Pull the urllib Request object out of a captured ``urlopen`` call."""
    assert stack.call_count == 1, f"expected one urlopen call, got {stack.call_count}"
    args, _kwargs = stack.call_args
    return args[0]


def test_managed_tts_sends_heard_user_agent():
    from heard.tts.managed import ManagedTTS

    tts = ManagedTTS(token="fake_token_for_testing", base_url="https://api.heard.dev")

    fake_audio = b"\x00" * 256  # any non-empty bytes pass the size check

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return fake_audio

    with patch("urllib.request.urlopen", return_value=_Resp()) as mocked:
        tts.synth_to_file(
            "hello",
            "george",
            1.0,
            "en",
            Path("/tmp/heard_test_managed.mp3"),
        )

    req = _captured_request(mocked)
    ua = req.get_header("User-agent") or ""
    assert ua, "ManagedTTS sent no User-Agent header"
    assert "Heard" in ua, f"ManagedTTS User-Agent missing 'Heard' tag: {ua!r}"
    assert "urllib" not in ua.lower(), (
        f"ManagedTTS leaked the default Python-urllib UA: {ua!r}"
    )


def test_heard_api_post_sends_heard_user_agent():
    from heard import heard_api

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"ok":true}'

    with patch("urllib.request.urlopen", return_value=_Resp()) as mocked:
        heard_api._post_json("https://api.heard.dev/v1/auth/request", {"email": "a@b"})

    req = _captured_request(mocked)
    ua = req.get_header("User-agent") or ""
    assert ua, "heard_api._post_json sent no User-Agent header"
    assert "Heard" in ua, f"heard_api UA missing 'Heard' tag: {ua!r}"
    assert "urllib" not in ua.lower(), (
        f"heard_api leaked the default Python-urllib UA: {ua!r}"
    )


def test_persona_managed_rewrite_sends_heard_user_agent(monkeypatch):
    """The cloud-LLM rewrite path is async-loaded inside the function,
    so we patch urlopen at the module level and exercise the function
    via a fake config + persona."""
    from heard import persona

    monkeypatch.setattr(
        persona,
        "_managed_rewrite_available",
        lambda: True,
    )
    fake_cfg = {
        "heard_token": "fake_token",
        "heard_api_base": "https://api.heard.dev",
        "heard_plan": "trial",
        "heard_trial_expires_at": 9999999999999,
    }
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "")

    class _FakeConfig:
        @staticmethod
        def load():
            return fake_cfg

    import heard

    monkeypatch.setattr(heard, "config", _FakeConfig, raising=False)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"hi"}]}'

    captured = {}

    def fake_urlopen(req, *_a, **_kw):
        captured["req"] = req
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    p = persona.Persona(name="raw", voice="george", system_prompt="")
    out = persona._managed_haiku_rewrite(p, "final", "hi", "final_short", {}, {})
    assert out == "hi"

    ua = captured["req"].get_header("User-agent") or ""
    assert ua, "persona._managed_haiku_rewrite sent no User-Agent"
    assert "Heard" in ua, f"persona managed-rewrite UA missing 'Heard': {ua!r}"
    assert "urllib" not in ua.lower(), (
        f"persona managed-rewrite leaked default urllib UA: {ua!r}"
    )
