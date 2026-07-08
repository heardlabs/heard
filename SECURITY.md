# Security Policy

## Reporting a vulnerability

Please report security issues privately — do **not** open a public
GitHub issue for a vulnerability.

- Preferred: open a private advisory via GitHub Security Advisories
  ("Report a vulnerability") on the [heardlabs/heard](https://github.com/heardlabs/heard)
  repository.
- Or email **security@heard.dev** with details and reproduction steps.

We aim to acknowledge reports within a few business days. Please give us
a reasonable window to ship a fix before any public disclosure.

## Supported versions

Heard ships as a rolling macOS app; fixes land in the latest release.
Please reproduce on the current version before reporting.

## Scope

In scope:

- The macOS app / daemon (`heard/`) and the CLI.
- The local IPC surface (see trust boundaries below).
- The `heard://` custom URL scheme handler.
- The client side of the network paths to `api.heard.dev`, ElevenLabs,
  and Anthropic.

Out of scope:

- The `api.heard.dev` managed backend infrastructure (report those to
  the same contact, but they are a separate service).
- Third-party services (ElevenLabs, Anthropic, PostHog, GitHub) — report
  issues in their code to those vendors.
- Vulnerabilities that require an already-compromised local user account
  (an attacker who is the logged-in macOS user can already read the
  config and socket by design — see below).

## Trust boundaries

Heard runs as a per-user macOS app. Its security-relevant boundaries:

1. **Local Unix-domain socket.** The daemon listens on a Unix-domain
   socket at `~/Library/Application Support/heard/daemon.sock`. Hook
   subprocesses (`python -m heard.hook <agent>`) and the CLI send JSON
   commands to it (narration events, `status`, `mute`, `feedback`, …).
   The socket is filesystem-scoped to the user account; it is **not** a
   network listener. Any process running as the same user can talk to
   it — that is the intended trust model for a single-user desktop tool.

2. **`heard://` custom URL scheme.** Registered via `CFBundleURLTypes`
   and handled in `heard/url_scheme.py`. It answers exactly one shape —
   `heard://auth?code=…` (or `?token=…`) — the tail of the web Google
   sign-in handoff, which claims a one-time install code for a bearer
   token. Other `heard://` payloads are ignored. Because any local app
   can open a URL, the handler must stay strict: it should never perform
   destructive or arbitrary actions from URL input.

3. **Cloud TTS / LLM path.** Depending on configuration, agent output
   text leaves the machine over HTTPS:
   - **TTS:** to ElevenLabs directly (BYOK `elevenlabs_api_key`), or via
     the managed proxy at `api.heard.dev` when signed in.
   - **Narration brain (LLM):** to Anthropic directly (BYOK
     `ANTHROPIC_API_KEY`), or via the managed `api.heard.dev` proxy.
   - **Auth / usage:** `api.heard.dev` for sign-in and plan/usage status.
   See `PRIVACY.md` for exactly what data crosses this boundary.

Local secrets (BYOK API keys, the Heard bearer token) live in
`~/Library/Application Support/heard/config.yaml`, readable by the user
account that owns them.
