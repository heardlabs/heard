# Configure Heard with YAML

Heard has two YAML configuration scopes:

- **Global configuration** changes Heard for every agent and project on this
  Mac.
- **Project configuration** changes narration for events whose working
  directory is inside one project.

Use the menu bar for everyday changes. Use YAML when you need settings that are
not exposed there, want a reproducible project setup, or need a nested override
for one part of a repository.

## Choose global or project scope

| If you want to… | Use… |
|---|---|
| Pick the default persona, voice, mode, or privacy settings for Heard | Global `config.yaml` |
| Store provider keys or control an app-wide service | Global `config.yaml` |
| Make one repository quieter, faster, or use a different persona | Project `.heard.yaml` |
| Name an agent's feature area within a repository | Project `.heard.yaml` `label` |
| Change how the narration brain phrases one project's updates | Project `.heard.yaml` `preferences` |

There is no special global `.heard.yaml`. The global file is named
`config.yaml`; `.heard.yaml` always means a project file.

## Global configuration

Ask Heard for the global file's location instead of assuming a macOS path:

```bash
heard config path
```

The safest way to change one global value is the CLI. It validates common
values, writes `config.yaml`, and asks the running daemon to reload:

```bash
heard config set persona aria
heard config set mode companion
heard config set speed 1.0
heard config set auto_silence_on_mic true
```

You can also edit the file directly. A small global file might look like this:

```yaml
persona: aria
mode: companion
verbosity: brief
speed: 1.0
auto_silence_on_mic: true
```

You do not need to copy every default into the file. Heard starts with its
built-in defaults and overlays only the values you set. The CLI also omits
values that equal their built-in defaults when it saves the file.

The CLI currently coerces booleans only for `narrate_tools`,
`narrate_tool_results`, `hotkey_enabled`, `auto_silence_on_mic`,
`auto_resume_on_mic_release`, `multi_agent_digest_enabled`, and
`multi_agent_auto_voices` (plus the internal `onboarded` key). For other
boolean controls in this guide—such as `product_analytics`, `byok_telemetry`,
`update_check_enabled`, `auto_update`, `codex_enabled`, `narration_spool`, and
`harness_think_say`—edit global YAML with an unquoted `true` or `false` instead
of passing the word through `heard config set`.

## Project configuration

Create `.heard.yaml` at a repository root to override project-capable settings:

```yaml
# .heard.yaml
label: Payments API
persona: atlas
mode: focus
verbosity: quiet
narrate_tools: false

preferences:
  error_detail_level: verbose
  register_formality: formal
  tool_category_volume:
    bash: quiet
    edit: normal
```

The file is read from the working directory attached to each agent event. It
does not change the daemon's app-wide provider, account, hotkey, updater, or
service state.

Project files can live below the repository root too. For example:

```text
acme/
├── .heard.yaml
└── services/
    ├── api/
    │   └── .heard.yaml
    └── web/
```

An event from `acme/services/api/` uses `services/api/.heard.yaml`. An event
from `acme/services/web/` uses `acme/.heard.yaml`.

Only the **nearest** `.heard.yaml` is loaded. Project files are not merged with
other project files above them. If the nearer file contains only a `label`, it
does not inherit `persona`, `mode`, or `preferences` from the repository-root
file; those values fall back to the global layer or built-in defaults.

## How Heard resolves configuration

For ordinary top-level settings, later layers win:

```text
built-in defaults
  ↓
global config.yaml
  ↓
nearest .heard.yaml for the event's working directory
```

The merge is shallow. A mapping in a later layer replaces the same mapping in
an earlier layer rather than recursively merging it.

Narration preferences have a separate, validated stack:

```text
preference schema defaults
  ↓
global user preferences.yaml
  ↓
nearest .heard.yaml preferences: mapping
```

The `label` key is different again: Heard deliberately reads it only from the
nearest project file, never from global config.

Project overrides are resolved per event by the daemon. The settings marked
**Global + project** below are the reliable project surface. Timing filters
have an adapter caveat: Codex and `heard run` resolve `skip_under_chars` and
`flush_delay_ms` with project context, while the Claude Code hook performs its
early transcript filtering with global values before the event reaches the
daemon. Keep those two settings global if you use more than one adapter.

## Configuration reference

The reference below covers settings intended for manual configuration on the
open-source path. Heard's file also contains account state, onboarding markers,
update bookkeeping, and managed-build seams. Those internal values are written
by Heard and are not a supported configuration interface.

Scope meanings:

- **Global + project** — safe to set globally or override in `.heard.yaml`.
- **Global** — app-wide; set it in `config.yaml`, with the CLI, or in Settings.
- **Direct YAML** — recognized at runtime but not part of `config.DEFAULTS`, so
  `heard config set` will not persist it. See [Direct-YAML event filters](#direct-yaml-event-filters).

### Narration

| Key | Scope | Default / values | What it does |
|---|---|---|---|
| `persona` | Global + project | `jarvis`; `aria`, `atlas`, `friday`, `jarvis`, `raw`, or a custom persona name | Selects the narration personality. `raw` has no persona prompt. Custom Markdown personas live under the global Heard config directory in `personas/`. |
| `mode` | Global + project | `copilot`; `copilot`, `companion`, `focus` | Sets the listening mode. Co-pilot gives screen-on signposts, Companion is fuller for eyes-off use, and Focus speaks attention events only. |
| `verbosity` | Global + project | `normal`; `quiet`, `brief`, `normal`, `verbose` | Selects the fast-path verbosity profile. Legacy `low` and `high` still map to `quiet` and `verbose`. Custom profiles can live in the global config directory under `profiles/`. |
| `narrate_tools` | Global + project | `true` | Master switch for tool-start narration. Failures can still pierce some profile gates, but setting this to `false` prevents normal tool narration. A project can reliably make a globally enabled stream quieter; a project cannot restore Claude Code events that the hook already filtered because the global value was false. |
| `narrate_tool_results` | Global + project | `true` | Controls successful tool-result narration. Failure results remain important signals. The same Claude Code re-enable limitation as `narrate_tools` applies. |
| `harness_think_say` | Global + project | `true` | Lets the narration brain return a private `think` field alongside the spoken `say` field. The private field is logged for inspection and is never sent to TTS. |

### Voice and playback

The active TTS backend is selected globally. Project files may select the
voice, speed, and language used by that already-active backend; they cannot
switch providers or supply project-local credentials.

| Key | Scope | Default / values | What it does |
|---|---|---|---|
| `voice` | Global + project | `george` | ElevenLabs voice alias or 20-character voice ID. Built-in aliases are `adam`, `bill`, `charlotte`, `daniel`, `george`, `lily`, and `rachel`. A persona's own `voice` wins when set. |
| `kokoro_voice` | Global + project | `bm_george` | Kokoro voice ID used by the local backend. A persona's `kokoro_voice` wins when set. |
| `speechify_voice` | Global + project | empty, which resolves to `geffen_32` | Speechify Simba voice ID. Known curated IDs include `beatrice_32`, `dominic_32`, `edmund_32`, `geffen_32`, `harper_32`, `hugh_32`, `imogen_32`, and `wyatt_32`. A persona's `speechify_voice` wins when set. |
| `speed` | Global + project | `1.05`; CLI accepts `0.5` through `2.0` | Sets speech speed. Individual providers may clamp their native speed and Heard uses playback-rate adjustment for the remainder. A persona's configured speed can replace the global value when that persona is applied as a preset. |
| `lang` | Global + project | `en-us` | Language code passed to TTS. Speechify Simba is English-only. |
| `skip_under_chars` | Global | `30`; non-negative integer | Drops assistant prose shorter than this threshold before it is sent for narration. Codex and `heard run` can resolve it per project, but global scope is portable across adapters. |
| `flush_delay_ms` | Global | `800`; non-negative integer | Waits for an agent transcript to flush before reading it. Codex can resolve it per project; keep it global for consistent behavior across adapters. |

### Multi-agent behavior

Multi-agent scheduling is daemon-wide. Keep these values global even though a
generic project file can syntactically contain them.

| Key | Scope | Default / values | What it does |
|---|---|---|---|
| `multi_agent_digest_enabled` | Global | `true` | Allows background-agent events to accumulate into project summaries. When false, the scheduler discards pending background events instead of speaking them. |
| `multi_agent_auto_voices` | Global | `false` | Gives agents distinct voices from the active backend's pool. When false, agents share the persona voice and Heard uses spoken labels when disambiguation is needed. |
| `multi_agent_voice_scope` | Global | `project`; `project` or `window` | `project` hashes repository names to stable voices and leaves the focus agent on the persona voice. `window` assigns every session, including focus, a round-robin voice that may change after restart. |
| `agent_voices` | Global | `{}` | Manual repository-name-to-voice-ID mapping. Manual entries win over automatic assignment. IDs must belong to the active provider's voice namespace. |

Example:

```yaml
multi_agent_auto_voices: true
multi_agent_voice_scope: project
agent_voices:
  api: 21m00Tcm4TlvDq8ikWAM
  web: XB0fDUnXU5powFXDhCwa
```

### Microphone and hotkeys

| Key | Scope | Default / values | What it does |
|---|---|---|---|
| `auto_silence_on_mic` | Global | `true` | Stops narration while another app captures the microphone and resumes normal handling after release. Set `false` if Heard should continue speaking during calls or dictation. |
| `hotkey_enabled` | Global | `true` | Enables the global pause and continue shortcuts. |
| `hotkey_pause` | Global | `"<shift>+<alt>+."` | pynput-style key chord for pausing Heard. |
| `hotkey_continue` | Global | `"<shift>+<alt>+,"` | pynput-style key chord for continuing Heard. |

`voice_mode`, `push_to_talk`, `push_to_talk_socket`, `voice_cleanup`, and the
voice-service settings belong to Heard Power's input service. They are not
open-source `.heard.yaml` controls. `push_to_talk` is derived from
`voice_mode`, so setting it independently is not stable.

### Providers

Provider selection and credentials are always global. Never put these keys in a
repository file.

| Key | Scope | Default | What it does |
|---|---|---|---|
| `elevenlabs_api_key` | Global | empty | Enables direct ElevenLabs TTS on the self-host path. If both voice-provider keys are set, ElevenLabs has priority over Speechify. |
| `speechify_api_key` | Global | empty | Enables direct Speechify Simba TTS when no honored ElevenLabs key is active. |
| `anthropic_api_key` | Global | empty | Enables the direct Anthropic narration brain. `ANTHROPIC_API_KEY` is the environment fallback; the config value wins. |
| `brain_model` | Global | empty | Experimental Anthropic model override for the BYOK narration path. Empty uses Heard's pinned default model. `HEARD_BRAIN_MODEL` is the environment fallback; config wins. |
| `openai_api_key` | Global | empty | Compatibility key used by older persona-rewrite paths. The mandatory narration harness uses Anthropic BYOK or Heard's managed proxy, so this key does not select the main narration brain. |

On a self-hosted install with no active managed account, voice backend priority
is ElevenLabs key, Speechify key, downloaded local Kokoro model, then no-audio
`NullTTS`. Managed builds can insert their signed-in cloud voice before local
Kokoro and apply account-level BYOK rules.

### Updates and privacy

| Key | Scope | Default | What it does |
|---|---|---|---|
| `update_check_enabled` | Global | `true` | Enables the anonymous daily GitHub Releases check. Set false to stop the check entirely. |
| `auto_update` | Global | `true` | Allows the menu app to download and stage an available update in the background for the next launch. A server-mandated minimum version may still force an immediate update in managed builds. |
| `product_analytics` | Global | `true` | Controls anonymous PostHog product analytics. When false, the client sends no product-analytics events. Narration text, project paths, file names, and function names are not included. |
| `byok_telemetry` | Global | `true` | For a signed-in user synthesizing through direct ElevenLabs or local Kokoro, reports character counts and backend name to the Heard usage endpoint. It does not send narration content. Managed synthesis is counted server-side and is not double-reported; the current Speechify path is not included in this client report. |
| `codex_enabled` | Global | `true` | Enables Codex Desktop observation independently of whether the Codex CLI hook file is installed. |

### Advanced self-hosting

| Key | Scope | Default | What it does |
|---|---|---|---|
| `narration_spool` | Global | `false` | Writes synthesized audio and text to the global config directory's `narration-out/` folder for an external renderer. When Heard is muted and spooling is enabled, synthesis can continue for the external consumer while local playback stays silent. |

### Direct-YAML event filters

These event filters are read by the current daemon but are not members of
`config.DEFAULTS`. Edit them directly in YAML; `heard config set` cannot
persist them and a later CLI or Settings save can remove unknown keys from the
global file. They are most useful in `.heard.yaml`, where Heard never rewrites
the file.

| Key | Scope | Runtime default | What it does |
|---|---|---|---|
| `notify_errors` | Direct YAML; global + project | `true` | When false, suppresses events tagged as errors or failures. |
| `notify_blocked` | Direct YAML; global + project | `true` | When false, suppresses agent questions that look like approval, review, confirmation, or decision requests. Ordinary clarification questions still speak. |
| `notify_completions` | Direct YAML; global + project | `true` | When false, suppresses final/completion narration for that scope. |
| `announce_project_switch` | Direct global YAML only | `true` | When false, disables “Now on …” tags when speech switches projects in a multi-agent session. The speech-drain path reads daemon-global state, so a project override does not affect it. |

### Persisted keys that are not controls

Do not manually set account tokens, plan/email fields, install IDs, first-launch
timestamps, onboarding flags, greeting state, update-attempt markers,
connection-hint dismissal flags, phone-pairing state, managed API URLs, or
managed-build service commands. Heard owns their lifecycle and may overwrite
them.

Several older keys remain in the file for compatibility but do not control the
current path:

- `harness_enabled` is ignored because the narration brain is mandatory.
- `auto_resume_on_mic_release` is ignored; mic-release resume is fixed behavior.
- `narrate_prompt_intent` can stop the hook from emitting a prompt-intent event,
  but the daemon retires those events without speaking them either way.
- `swarm_verbosity` is declared for compatibility but the active router does
  not read it.
- `multi_agent_digest_interval_s` is declared but the current channel scheduler
  uses its own idle and backpressure rules instead.

Unknown YAML keys may be loaded into the in-memory dictionary, but they do
nothing unless current code reads them.

## Project labels

`label` gives an agent a human feature or area name inside a project:

```yaml
label: Checkout migration
```

Heard uses the nearest label in its agent-state context so concurrent agents in
one repository can be distinguished as “Checkout migration” and “Admin UI”
instead of both being called only by the repository name. The label is stored
locally and is not sent in product analytics. Because it is narration context,
it may be included in a request to your configured narration provider.

`label` is project-only. A `label` in global `config.yaml` is ignored. A nested
`.heard.yaml` label wins because only the nearest project file is read. Heard
resolves the label when it first observes an agent session, so start a new agent
session or restart the daemon after changing a label for an existing session.

## Narration preferences

Narration preferences shape brain-routed prose and final updates. They normally
change the format of failures and user questions rather than whether those
events are heard; the separate direct-YAML event filters can suppress an event
category before it reaches the brain. Set project preferences under the
`preferences:` key:

```yaml
preferences:
  long_final_shape: lead_then_summary
  jargon_translation: aggressive
  question_handling: verbatim
```

Set a global user preference with the CLI:

```bash
heard preferences set register_formality casual
```

That command writes the separate user-level `preferences.yaml`, not
`config.yaml`. Mapping preferences cannot currently be set through the CLI;
edit the file printed by `heard preferences path` or put the mapping in a
project `.heard.yaml`.

All schema-version-1 preferences are:

| Preference | Default | Accepted values | Effect |
|---|---|---|---|
| `tool_category_volume` | `{}` | Map `bash`, `edit`, `read`, `web`, or `agent` to `quiet`, `normal`, or `verbose` | Changes narration volume for selected tool categories; absent categories inherit normal behavior. |
| `routine_tool_progress` | `brief` | `skip`, `brief`, `full` | Chooses whether routine tool starts are skipped, acknowledged briefly, or narrated in full. |
| `intermediate_prose_threshold` | `240` | Integer `80` through `1000` | Tells the narration brain how readily short intermediate prose should count as substantive. The current deterministic fast-path boundary remains code-defined. |
| `long_final_shape` | `preserve_structure` | `preserve_structure`, `lead_then_summary`, `headline_only` | Chooses how aggressively a long final answer is compressed. |
| `decision_surfacing` | `emphasize` | `emphasize`, `mention`, `skip` | Controls how much of an agent's decision and rationale is voiced. |
| `jargon_translation` | `moderate` | `aggressive`, `moderate`, `preserve` | Controls how strongly developer jargon is translated into plain language. |
| `register_formality` | `neutral` | `formal`, `neutral`, `casual` | Adjusts formality within the selected persona without changing persona identity. |
| `hook_endings` | `preferred` | `required`, `preferred`, `optional` | Controls how often narration ends with a question or next-action hook. |
| `error_detail_level` | `standard` | `minimal`, `standard`, `verbose` | Controls error narration detail, not whether errors are announced. |
| `question_handling` | `verbatim` | `verbatim`, `summarize`, `acknowledge` | Controls whether an agent question is read, shortened, or only acknowledged. |

Preferences are instructions included in the narration brain's prompt. The
deterministic fast path for routine tool templates does not read this schema
directly, so `tool_category_volume`, `routine_tool_progress`, and
`intermediate_prose_threshold` do not rewrite the fast-path routing thresholds
in current `main`.

Invalid preference names, types, mappings, or enum values are ignored at read
time and resolution falls through to the user or schema-default layer.

Inspect preferences and their sources with:

```bash
heard preferences list
heard preferences list --cwd /path/to/project
heard preferences explain register_formality
heard preferences why register_formality
heard preferences get register_formality
heard preferences path
```

`list --cwd` is the command that can inspect an arbitrary project path. The
other source-sensitive commands use the shell's current working directory.

## Keep credentials out of project files

A project's `.heard.yaml` is an ordinary repository file. It may be committed,
copied, reviewed, or published. Never place these values in it:

- `anthropic_api_key`
- `openai_api_key`
- `elevenlabs_api_key`
- `speechify_api_key`
- any `*_token`, `*_secret`, account field, or private service URL

Store credentials in global `config.yaml`, or use the documented environment
variables for the narration provider. If you intentionally keep a private,
untracked `.heard.yaml`, add it to the repository's ignore rules before writing
anything sensitive—but global config is still the safer design.

## Apply and inspect changes

Inspect the global layer:

```bash
heard config path
heard config get
heard config get persona
```

The full `heard config get` listing redacts keys whose names end in `_api_key`,
`_token`, or `_secret`. Asking for one key by name intentionally prints that
value in full. Avoid pasting the output of a single-key credential lookup into
issues or chat.

Apply a validated global change:

```bash
heard config set verbosity brief
```

`heard config set` reloads a running daemon automatically. Project
`.heard.yaml` and project preferences are read again for subsequent events, so
they do not need a daemon restart. Project `label` is the exception because it
is cached in agent state for the session; start a new session or restart after
changing it.

After editing global YAML directly, restart Heard from the menu bar, or stop the
daemon from a shell:

```bash
heard stop
```

The next agent event starts it again and loads the edited global file.

YAML types matter. Use real booleans and numbers:

```yaml
narrate_tools: false
speed: 1.0
```

Do not quote them as `"false"` or `"1.0"`. Quote strings that YAML might
otherwise coerce—especially words such as `on`, `off`, `yes`, or `no`—and quote
hotkey strings containing punctuation. The root of either YAML file must be a
mapping of keys to values, not a list or a scalar.

## Troubleshooting

### My project override does not apply

1. Confirm the agent event's working directory is inside the expected project.
2. Look for a nearer `.heard.yaml`; only the first one found while walking
   upward is loaded.
3. Remember that the nearer project file replaces, rather than extends, the
   repository-root project layer.
4. Use `heard preferences list --cwd /exact/project/path` for preference
   sources. `heard config get` shows only defaults plus global config; it does
   not resolve a project's top-level overrides.
5. Check that the key is marked **Global + project** or **Direct YAML; global +
   project** in this guide.

### Heard ignored a malformed project file

Heard never renames or rewrites a repository's `.heard.yaml` after a parse
error. It reports the parse problem to stderr, ignores that project layer, and
continues with global/default configuration. Fix the project file yourself and
let the next event reload it.

### Heard renamed my global file

When global `config.yaml` has invalid YAML syntax, Heard moves it to
`config.yaml.broken-<timestamp>` and starts from defaults so a hand-edit cannot
brick app launch. Recover the values you need from that backup, correct the
syntax, and write a valid `config.yaml`.

### A value saved with the wrong type

Prefer `heard config set` for keys it validates. When editing YAML directly,
check indentation and scalar types. A quoted boolean is a non-empty string and
may behave as true in Python even when its text says `"false"`.
