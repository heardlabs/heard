# Privacy

Heard is an ambient tool: it watches your coding agent's activity and
speaks a narration of it. That means it reads agent transcript / output
text, and — to turn that text into speech and into a spoken summary — it
sends some of that text to cloud services. This document describes
plainly what stays on your machine, what leaves it and to whom, what
telemetry is collected, and how to turn each of these off.

This is honest disclosure, not marketing. If any of it is inaccurate,
please open an issue.

## What stays on your machine

These never leave your Mac. They live under
`~/Library/Application Support/heard/`:

- **`history.jsonl`** — the log of every utterance Heard spoke, with a
  unique `id` per record and sibling `type="feedback"` records.
- **`config.yaml`** — your settings, including any BYOK API keys and your
  Heard sign-in token.
- **Spoken-text hashes** (`spoken.py`, per-session `<session>.json`) —
  used to avoid re-narrating the same assistant text. Deduplication
  bookkeeping only.
- **`defect_reports.jsonl`** — "Report a problem" reports. This sidecar
  is **local-only; it makes no network calls.**
- **`daemon.log`** — the structured local event log (10 MB rotation).

## What leaves your machine, and to whom

To narrate, Heard sends text over HTTPS to third parties. Which providers
depends on your configuration:

- **Text-to-speech (the audio).** The text to be spoken is sent to a TTS
  provider so it can be synthesized into audio:
  - **ElevenLabs** directly, if you configured a BYOK
    `elevenlabs_api_key`; or
  - the **managed proxy at `api.heard.dev`** when you're signed in
    (which forwards to the TTS provider on your behalf); or
  - **nothing leaves** if you use the local **Kokoro** voice — it runs
    fully on-device (opt-in download).
- **Narration brain (the LLM).** For prose and finals, a distilled
  summary of the agent's output is sent to an LLM to produce the spoken
  line. This goes to **Anthropic** directly (BYOK `ANTHROPIC_API_KEY`),
  or via the managed **`api.heard.dev`** proxy when signed in.
- **Auth & usage status.** `api.heard.dev` is contacted for sign-in
  (exchanging an install code for a bearer token), token refresh, and
  plan / usage lookups.

**Plainly: your agent's output text is sent to the TTS provider
(ElevenLabs, or the managed proxy) and to the narration LLM (Anthropic,
or the managed proxy).** If that is not acceptable for a given project,
use the local Kokoro voice and a BYOK LLM you trust — or pause Heard.

## Telemetry & analytics

Heard has two independent telemetry streams. Both **default ON** with a
one-time disclosure (an opt-**out** posture), and each has its own switch.
When you opt out, the stream is fully silenced — no event bypasses the flag.

### 1. Product analytics (PostHog)

Implemented in `heard/analytics.py`; events are POSTed to PostHog
(`us.i.posthog.com`). **Gated entirely by the `product_analytics` config flag
(default `True`).** It's on by default, but when you turn it off **nothing**
fires — no event class bypasses the flag. Events are anonymous (tied to a
per-install UUID, or your signed-in user id after sign-in). The event
classes:

- App lifecycle: `app_first_launched`, `app_launched`, `app_updated`,
  `app_crashed`.
- Onboarding funnel: `wizard_viewed`, `wizard_completed`,
  `wizard_abandoned`.
- Setup: `hook_installed`, `hook_uninstalled`, `greeting_played`.
- Quality signals: `synth_failed`, `audio_cutoff_detected`,
  `harness_fallback`, `defect_reported`.
- Lifecycle: `signin_completed`, `trial_started`, `plan_changed`.
- Usage: `narration_spoken` (sampled ~1:10), `narration_played_today`
  (at most once per local day), `setting_changed`, `session_*`.

Event **properties are categorical / anonymized** — no narration text,
no project paths, no file or function names. Device enrichment is coarse
(macOS version, CPU arch, locale). If you sign in and provide an email,
only a salted hash of it is sent, never the raw address.

> Signed-in conversions (sign-in, trial, plan changes) are also recorded
> server-side by `api.heard.dev` when you use a managed account — that's
> inherent to having an account and is independent of this local flag.

### 2. BYOK usage telemetry

Gated by the `byok_telemetry` config flag (default `True` = opt-out). After a
successful BYOK or local synth Heard reports **character counts only — never
content** — to `api.heard.dev/v1/telemetry/usage`, so your account dashboard's
usage heatmap reflects real usage. Managed-cloud synths are counted
server-side and skipped here. Turn it off and nothing is reported.

## Turning telemetry off

Both default ON; opt out at any time and the stream is fully silenced.

- **Product analytics:** toggle `product_analytics` in
  **Settings → Advanced → Privacy**, or
  `heard config set product_analytics true|false`.
- **BYOK usage telemetry:** toggle `byok_telemetry` in Settings, or
  `heard config set byok_telemetry true|false`.
- **All cloud text egress (TTS + LLM):** use the local **Kokoro** voice
  (Options → Download voice) and a BYOK LLM you control, or **pause
  Heard** from the menu bar so nothing is narrated at all.

## Contact

Privacy questions: **privacy@heard.dev**. Security issues: see
[`SECURITY.md`](./SECURITY.md).
