# Grokbot

**Status:** tested end to end in the United Kingdom. A Grokbot set up a bridge from the repository link, with the owner only signing in, choosing and listening, and spoken requests were answered on the owner's Echos.

## Capabilities

| Requirement | Grokbot feature | Notes |
| --- | --- | --- |
| Answering 1: authenticated HTTPS webhook | A routine with a **webhook trigger** | Grokbot generates the URL and key. It can't read them itself; the owner copies them into secret cards |
| Answering 2: a 2xx reply within 2 seconds | The trigger acknowledges with 200 and runs the routine afterwards | Proven |
| Answering 3: runs unattended | Routine runs | No approval was asked for the callback to `amazonalexa.com` |
| Answering 4–5: one callback, tokens kept private | Followed from the routine instructions | Proven |
| Setup 1: shell | The bot's own computer: Debian Linux with Python 3, Node 20, npm, git and passwordless `sudo` | Files survive between sessions. Survival across a computer restart is unverified; Update, Recover and Reset remove installed packages, which `bridge preflight` reinstalls |
| Setup 2: browser handover | The bot's browser with **take control** | The bot reads the sign-in redirect from its own address bar |
| Setup 3: secret store | **Secure secret cards** (“Stored securely, never shown to your Bot”), available to commands as environment variables | Proven |
| Setup 4: saved instructions | **Saved skills**, stored as `SKILL.md` files | Used for `alexa-bridge-maintenance`; the body is kept word for word |

## How Grokbot does each setup step

These sections match the “see your profile” points in [SETUP.md](../SETUP.md).

### Agent name

Use `--agent "Grokbot"`.

### Working folder

Use `/workspace/alexa-bridge` as `<work>`, cloned so that it is the repository root. Don't clone into a subfolder of it; an earlier setup did, and skills then had to search for the checkout.

### Browser handover

Open the page in your own browser and offer the owner **take control**. Wait until they say they're done, then take control back. For the Amazon sign-in, read the `maplanding` address from your own address bar straight into the private file; never paste it into chat.

### Connecting the routine

1. Create the routine on yourself, named “<Display name> Alexa bridge”, with a **webhook trigger**, and paste the `routine-text` instructions exactly.
2. You can't read the routine's webhook URL and key yourself. Post the routine's two field links (Webhook URL, Webhook key) and two **secure secret cards** named `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`.
3. Ask the owner to click each field link, copy the value and paste it into its card. The cards make both values available to your commands as environment variables without showing them to you.
4. Confirm only the URL's hostname and the key's length.

### Approval prompts

In testing, no approval was asked for the routine's POST to an `amazonalexa.com` host. If your approval settings ever ask, ask the owner to choose **Always allow** for that host.

### Saving the maintenance instructions

When setup finishes, use your skill save tool with the `name` and `description` from [`skills/alexa-bridge-maintenance/SKILL.md`](../skills/alexa-bridge-maintenance/SKILL.md) and its body word for word. The tool rebuilds the header and drops any other header fields, which is fine. If an “Alexa bridge maintenance” skill already exists, rewrite it in place by its id instead of adding a second one. In a trial on 8 October 2026, Grokbot stored a skill like this with its body word for word.

To remove the bridge later, also delete the routine, both secret cards and this skill.

## Known differences

None recorded.
