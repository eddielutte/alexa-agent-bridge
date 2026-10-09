# Agent profiles

Each profile says how one agent meets the [requirements](../AGENT-REQUIREMENTS.md). The [setup guide](../SETUP.md) is the same for every agent; it says what must happen and points to the profile for how.

| Agent | Status | Profile |
| --- | --- | --- |
| Grokbot | Tested end to end (United Kingdom) | [grokbot.md](grokbot.md) |

## Writing a profile

Name the file `<agent>.md`, in lower case. Use these sections:

1. **Status:** tested or untested, and what was proven, for example “a spoken request answered on an Echo”.
2. **Capabilities:** one row per requirement, numbered as in [AGENT-REQUIREMENTS.md](../AGENT-REQUIREMENTS.md), with the agent's feature and any evidence or caveat. Mark anything not yet tried as unverified.
3. **How the agent does each setup step:** one subsection for each “see your profile” point in the setup guide, with these headings:
   - **Agent name:** the spoken name to pass as `--agent`.
   - **Working folder:** where to clone the repository, if not `~/alexa-bridge`.
   - **Browser handover:** how the owner takes over the browser and hands it back.
   - **Connecting the routine:** creating the webhook-triggered routine and storing its URL and key as `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`.
   - **Approval prompts:** whether the routine's callback needs approving, and how to allow it permanently.
   - **Saving the maintenance instructions:** how to save [MAINTENANCE.md](../MAINTENANCE.md) for later.
4. **Known differences:** anything the owner should expect that other agents don't need.

Never include personal details, webhook URLs or keys, or account identifiers.
