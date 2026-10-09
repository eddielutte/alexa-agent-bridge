# Skills

Two skills in the open [Agent Skills](https://agentskills.io) format. Both are deliberately short: they point the agent to the instructions in the installed release (`SETUP.md` or `MAINTENANCE.md`), so they never go out of date when the bridge is updated.

| Skill | Use | Status |
| --- | --- | --- |
| [`alexa-bridge-maintenance`](alexa-bridge-maintenance/SKILL.md) | Check, re-sign-in, update or remove the bridge | Saved by every agent at the end of setup. Grokbot stores this kind of skill word for word (trial, 8 October 2026) |
| [`alexa-bridge-setup`](alexa-bridge-setup/SKILL.md) | Start setup from the skill instead of pasting the repository link | **Untested.** Grokbot owners paste the repository link instead |

## Installing

- **Grokbot:** nothing to install. Setup step 7 saves the maintenance skill; see the [Grokbot profile](../agents/grokbot.md).
- **Agents that load `SKILL.md` folders** (untested with the bridge): copy a skill's folder into the agent's skills folder, for example `~/.claude/skills/` for Claude Code or `~/.agents/skills/` for Codex (paths from each tool's documentation, October 2026). Then ask the agent to set up the Alexa bridge, giving it the repository link and release tag.

A skill must never carry setup steps of its own. Change `SETUP.md` or `MAINTENANCE.md` instead.
