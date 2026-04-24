# Hotkeys

The single highest-leverage binding is **`heard silence`** — it cuts Heard off
mid-sentence without killing the daemon, so the next response starts speaking
in ~300ms. Bind it to something your fingers already know (Cmd+., Cmd+Esc).

## Karabiner-Elements

Add this complex modification rule (Karabiner-Elements → Complex Modifications
→ Add rule → Add your own rule):

```json
{
  "description": "Heard: silence on Cmd+.",
  "manipulators": [
    {
      "type": "basic",
      "from": { "key_code": "period", "modifiers": { "mandatory": ["left_command"] } },
      "to": [{ "shell_command": "/opt/homebrew/bin/heard silence" }]
    }
  ]
}
```

Replace the path if `heard` is installed elsewhere (`which heard`).

## BetterTouchTool

1. Open BTT, click **Keyboard** at the top.
2. **Add New Shortcut**, set the trigger to `Cmd+.`.
3. Add action: **Execute Terminal Command (blocking)**.
4. Command: `/opt/homebrew/bin/heard silence`

## Hammerspoon

Drop this in `~/.hammerspoon/init.lua`:

```lua
hs.hotkey.bind({"cmd"}, ".", function()
  hs.execute("/opt/homebrew/bin/heard silence")
end)
```

Then reload Hammerspoon.

## Raycast

Raycast → Extensions → Script Commands → **New Script Command** → Shell:

```sh
#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Silence Heard
# @raycast.mode silent
heard silence
```

Assign a hotkey in Raycast's command list.

## Troubleshooting

- If `heard silence` does nothing, check `heard status`. If the daemon is not
  running, there's nothing to silence — the next speak request will start a
  fresh daemon.
- If the binding works once then stops, the daemon likely died. Run
  `heard service install` to have macOS keep it alive on login.
