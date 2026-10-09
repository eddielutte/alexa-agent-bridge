# What your agent needs

The bridge works with any AI agent that has the capabilities below. The agent does two jobs. One agent may do both, or the answering routine may run on a separate platform, such as an automation service:

- the **answering routine** answers each spoken request
- the **setup agent** builds the skill and keeps it working

How a particular agent provides each capability is described in its profile in [`agents/`](agents/README.md). **Tested with:** Grokbot ([profile](agents/grokbot.md)). Other agents should work if they meet every requirement, but are untested.

## Answering routine

The skill sends each request to one webhook. The routine must:

1. **Accept an authenticated HTTPS webhook.**
   - The URL is public HTTPS on port 443, with no user name or password in it.
   - The request is a `POST` with `Authorization: Bearer <key>` and a JSON body: `request_id`, `message` (the owner's words) and `reply` (where and how to send the answer).
   - **Platforms that authenticate differently:** if the platform's webhook carries its secret in the URL instead, put the full URL (with the secret) in `BRIDGE_WEBHOOK_URL` and store any non-empty value as `BRIDGE_WEBHOOK_KEY`. The Bearer header is then simply ignored. Platforms that require a custom header name or HMAC-signed requests aren't supported yet.
2. **Reply with any 2xx status (such as 200, 202 or 204) within 4 seconds, before doing the work.**
   - Any other status, including a redirect, or no reply in time, counts as "not confirmed". The skill tells the owner and never resends.
   - The routine runs the work after it has acknowledged.
3. **Run unattended.** No person approves the run or its outgoing call while the owner waits at the Echo.
4. **Post the answer exactly once, within 120 seconds,** to the official Alexa Skill Messaging address given in `reply`. The routine, or a tool it uses, must be able to send its own HTTPS request with custom headers and count UTF-8 bytes; a fetch tool that can't set headers isn't enough.
   - one HTTPS `POST` with the per-request bearer token from `reply`
   - a non-empty answer, without control characters other than line breaks and tabs, within 4,000 characters and the 6,000-byte data limit, shortened by rewriting rather than cut off; the skill drops anything else
   - no retry after a timeout or uncertain result, and no redirects
5. **Keep tokens out of chat and logs.** The request's tokens, and the callback address, are never shown or stored.

### The routine protocol

[`routine-template.md`](routine-template.md) is the protocol. `python3 -m bridge routine-text` prints it filled in for the owner's skill: its name, language, the allowed Skill Messaging hosts and the limits.

- **An AI agent** follows the printed text as the routine's instructions.
- **An automation platform** that doesn't follow written instructions (n8n, Zapier and similar) implements the same rules as a workflow: check `reply`, produce the answer, and make the one callback `POST`. The answer still has to come from something that can do the owner's task, such as an AI step in that workflow.

## Setup agent

The setup agent follows [the setup guide](SETUP.md). It must have:

1. **A Linux or macOS shell it can run commands in.** On Windows, use WSL (untested). ASK CLI's developer sign-in (`ask configure`) asks questions in the terminal. If the agent's shell can't answer them, the owner runs that one command in a terminal on the agent's computer.
   - Python 3.9 or later, Node.js with npm, and git. `bridge preflight` checks these and installs ASK CLI 2.30.7 without root. Release signatures are checked by the bridge itself, so `ssh-keygen` isn't needed.
   - Outbound HTTPS to Amazon's sites.
   - Files that stay put between steps. Setup state lives in `~/.alexa-bridge`. Everything resumes after a reset; `preflight` reinstalls what's missing.
2. **A web browser on the same computer, which it can hand to the owner.** Amazon's developer sign-in returns to a local address (`127.0.0.1`), so a browser elsewhere won't work. The owner signs in to Amazon, enters 2-step codes and solves any CAPTCHA themselves; the agent takes control back afterwards. The agent then reads the final sign-in address from its own address bar into a private file without displaying it. If it can't, the owner copies the address and the agent runs `signin finish --from-clipboard`, or `--from-prompt` for a hidden prompt where there's no desktop clipboard.
3. **A secure store for secrets (recommended).** The routine's webhook URL and key reach setup as the environment variables `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`, ideally from a store whose values the agent can use but never sees.
   - **Fallback:** the owner types both values into hidden prompts with `python3 -m bridge webhook-set`. This needs the owner to have a terminal on the agent's computer.
4. **A way to save reusable instructions,** such as a saved skill or task, so the owner can later ask for “Alexa bridge maintenance” using [MAINTENANCE.md](MAINTENANCE.md).

## What every agent must follow

These come from the setup guide's ground rules and apply to every agent:

- Use only this repository, at a release tag.
- Never type the owner's passwords, codes or CAPTCHAs, and never accept agreements for them.
- Never put secrets in chat or logs.
- Stop and ask when anything changes what the owner agreed to.

## Adding another agent

Write a profile in [`agents/`](agents/README.md) that maps each requirement above to the agent's feature, then follow [SETUP.md](SETUP.md) with that agent. Please report the result, including anything it couldn't do, so the profile can be marked tested.
