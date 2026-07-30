"""Per-agent voice pools follow the active TTS backend.

Regression cover for a bug that failed SILENTLY: the auto-assignment
pool held ElevenLabs voice IDs unconditionally, and the Speechify
backend maps unrecognised (ElevenLabs-shaped) IDs to its own default
rather than erroring. So on Speechify every agent in a swarm got the
same voice, with nothing in the log to say so.
"""

from __future__ import annotations

from heard.multi_agent import (
    _AUTO_VOICE_POOL,
    MultiAgentRouter,
    _auto_voice_for,
    voice_pool_for_backend,
)
from heard.tts.speechify import AUTO_VOICE_POOL as SPEECHIFY_POOL
from heard.tts.speechify import DEFAULT_VOICE_ID, _resolve_voice_id


def test_speechify_backend_gets_speechify_pool():
    assert voice_pool_for_backend("SpeechifyTTS") == SPEECHIFY_POOL


def test_other_backends_keep_elevenlabs_pool():
    for backend in ("ElevenLabsTTS", "ManagedTTS", "KokoroTTS", "NullTTS", ""):
        assert voice_pool_for_backend(backend) == _AUTO_VOICE_POOL


def test_elevenlabs_pool_ids_would_collapse_on_speechify():
    """The bug itself, pinned. Every ElevenLabs pool ID resolves to the
    SAME Speechify voice — so using the wrong pool silently destroys
    per-agent voices instead of failing loudly."""
    resolved = {_resolve_voice_id(v) for v in _AUTO_VOICE_POOL}
    assert resolved == {DEFAULT_VOICE_ID}


def test_speechify_pool_ids_survive_resolution():
    """The fix: each pool voice passes through as itself."""
    for v in SPEECHIFY_POOL:
        assert _resolve_voice_id(v) == v


def test_speechify_pool_excludes_the_default_voice():
    """The focus agent keeps the configured voice (the default). A
    background agent that hashed onto it would be indistinguishable
    from the agent you're actually driving."""
    assert DEFAULT_VOICE_ID not in SPEECHIFY_POOL


def test_pool_assignment_is_deterministic_per_repo():
    """Same repo → same voice across daemon restarts (SHA-1, not the
    per-process-salted builtin hash)."""
    for pool in (_AUTO_VOICE_POOL, SPEECHIFY_POOL):
        assert _auto_voice_for("web", pool) == _auto_voice_for("web", pool)


def test_distinct_repos_spread_across_the_pool():
    """Not a guarantee for any given pair, but the pool must actually be
    used — a mapping that returns one voice for everything is the bug
    this module exists to prevent."""
    repos = ["web", "api", "heard", "locl", "infra", "docs", "mobile"]
    voices = {_auto_voice_for(r, SPEECHIFY_POOL) for r in repos}
    assert len(voices) > 1
    assert voices <= set(SPEECHIFY_POOL)


def test_empty_repo_name_still_returns_a_pool_voice():
    assert _auto_voice_for("", SPEECHIFY_POOL) == SPEECHIFY_POOL[0]


def test_empty_pool_falls_back_rather_than_crashing():
    """Defensive: an empty pool must not IndexError inside the router's
    lock — that would take the narration thread down."""
    assert _auto_voice_for("web", ()) in _AUTO_VOICE_POOL


def test_router_defaults_to_elevenlabs_pool():
    assert MultiAgentRouter()._voice_pool == _AUTO_VOICE_POOL


def test_router_accepts_pool_at_construction():
    assert MultiAgentRouter(voice_pool=SPEECHIFY_POOL)._voice_pool == SPEECHIFY_POOL


def test_set_voice_pool_swaps_namespace_on_backend_change():
    """A config reload that re-picks the backend must re-point the pool;
    otherwise the daemon keeps assigning the old provider's IDs."""
    router = MultiAgentRouter()
    router.set_voice_pool(voice_pool_for_backend("SpeechifyTTS"))
    assert router._voice_pool == SPEECHIFY_POOL
    router.set_voice_pool(voice_pool_for_backend("ElevenLabsTTS"))
    assert router._voice_pool == _AUTO_VOICE_POOL


def test_set_voice_pool_ignores_empty():
    router = MultiAgentRouter(voice_pool=SPEECHIFY_POOL)
    router.set_voice_pool(())
    assert router._voice_pool == _AUTO_VOICE_POOL
