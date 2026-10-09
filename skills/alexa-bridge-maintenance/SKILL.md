---
name: alexa-bridge-maintenance
description: Check, repair, re-sign-in, update or remove the owner's Alexa bridge. Use when the owner asks about the Alexa bridge, or says Alexa reported that sign-in needs attention.
---
# Alexa bridge maintenance

The steps live in the bridge's own `MAINTENANCE.md`, in the release that is installed, so they always match it.

1. Find the bridge checkout. It's recorded as `checkout` in `~/.alexa-bridge/state.json`, which holds no secrets:
   `python3 -c "import json, os; print(json.load(open(os.path.expanduser('~/.alexa-bridge/state.json')))['checkout'])"`
   If that fails, use the folder in your working folder that contains both `bridge/` and `MAINTENANCE.md`.
2. Read `MAINTENANCE.md` in that folder and follow the section that matches the owner's request. Run its commands from that folder.
3. The setup ground rules still apply: never type the owner's credentials, never put secrets in chat or logs, and stop when something changes what the owner agreed to.
