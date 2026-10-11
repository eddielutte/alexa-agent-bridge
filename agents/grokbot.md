# Grokbot

**Status:** tested end to end in the United Kingdom. A Grokbot set up a bridge from the repository link, with the owner only signing in, choosing and listening, and spoken requests were answered on the owner's Echos.

## Capabilities

| Requirement | Grokbot feature | Notes |
| --- | --- | --- |
| Answering 1: authenticated HTTPS webhook | A routine with a **webhook trigger** | Grokbot generates the URL and key. It can't read them itself; the owner copies them into secret cards |
| Answering 2: a 2xx reply within 4 seconds | The trigger acknowledges with 200 and runs the routine afterwards | Proven. A first request after a deploy has taken more than 2 seconds, so the 4-second limit matters |
| Answering 3: runs unattended | Routine runs | No approval was asked for the callback to `amazonalexa.com` |
| Answering 4–5: one callback, tokens kept private | Followed from the routine instructions | Proven |
| Setup 1: shell | The bot's own computer: Debian Linux (x86-64) with Python 3, Node, npm, git and `libX11`. No root, no `ssh-keygen`, no tkinter and no clipboard tool | The bridge needs nothing more: it installs ASK CLI under `~/.alexa-bridge/tools` and reads the clipboard itself. Files survive between sessions. An Update, Recover or Reset of the computer removes installed packages; `bridge setup` reinstalls ASK CLI |
| Setup 2: browser handover | The bot's Chrome with **take control** (one-to-one chats only), and a browser helper that works through the screen, mouse and keyboard | The bot's computer runs **one Chrome per assistant**, each on its own X display, and none of them on the shell's display. On 10 October 2026, the window you see and the display your helper copies on weren't even the same one. So the bridge never guesses a display: you open both sign-in pages yourself, and it reads the clipboard on every display. Don't set `BRIDGE_DISPLAY` |
| Setup 3: secret store | **Secure secret cards** (“Stored securely, never shown to your Bot”), available to commands as environment variables | Proven. Grokbot can't delete them |
| Setup 4: saved instructions | **Saved skills**, stored as `SKILL.md` files | Used for `alexa-bridge-maintenance`; the body is kept word for word |

## How Grokbot does each setup step

These sections match the “see your profile” points in [SETUP.md](../SETUP.md).

### Agent name

Use `--agent "Grokbot"`.

### Working folder

Use `/workspace/alexa-bridge` as `<work>`, cloned so that it is the repository root. Don't clone into a subfolder of it.

### Owner questions

Run setup in the owner's **one-to-one** chat: take control doesn't work in group chats, and **New chat** can start one. Ask SETUP step 2's three questions in one message, using choice cards:
- the name, with your suggestion first and “pick another”
- the country
- yes or no for the Amazon account

Later, offer the test Echo, and any developer organisation, as choice cards too. Keep progress messages short; the owner needs only the stops' `say` text.

### Browser handover

Open the page in your Chrome and offer the owner **take control**. Wait until they say they're done, then take control back.

- **The speech sign-in.** Open the `signin_url` yourself. Amazon's last page says “Sorry, we couldn't find that page”, with a dog; tell the owner in advance that this means it worked. As soon as they hand back, your browser helper copies the address bar (Ctrl+L, Ctrl+A, Ctrl+C in Chrome). Then run `python3 -m bridge setup` straight away, because Amazon's code expires within minutes. The bridge reads the clipboard itself, on every display, so no clipboard tool is needed. Never paste the address into chat.
- **The developer sign-in.** `bridge setup` starts ASK CLI's sign-in in the background and prints `dev_signin_url`. Open that page yourself, as for the speech sign-in, and hand control; the owner signs in and clicks **Allow**. Amazon then shows a page whose address contains `ask-cli-no-browser.html`. Copy that address bar straight away and run `setup`; the bridge hands the code to ASK CLI. Proven on 11 October 2026: the copied address was on one of the computer's four displays, and the bridge's reader found it there. An Update, Recover or Reset of your computer can remove this sign-in; `setup` then asks for it again.

### Connecting the routine

1. Create the routine on yourself, named “<Display name> Alexa bridge”, with a **webhook trigger**, and paste the `routine-text` instructions exactly.
2. You can't read the routine's webhook URL and key yourself. In **one message**, post the routine's two field links (Webhook URL, Webhook key) and two **secure secret cards** named `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`. Say something like: “Two connection codes for the routine: click each link, copy, and paste into the matching card. I never see them.”
3. The links and cards are the only route for the codes, so the owner doesn't need to look anywhere else in the app; if they ask, post the links again.
4. Run `python3 -m bridge setup`. It sends the routine a setup check and confirms only the URL's base domain and the key's length. In a proof of concept on 11 October 2026, the routine replied 200 to the right key and 401 to a wrong one, and the run did nothing visible.
5. **Reinstalling:** you can't delete secret cards, so the cards from an earlier install are still there and still loaded. On 10 October 2026, setup accepted these old codes and connected the skill to a deleted routine. From v0.1.5 the setup check catches this (`webhook_unreachable`). On 11 October 2026, Grokbot answered 401 both for a wrong key and for a webhook address with no routine, so the check can't tell the two apart; a **deleted** routine's own address wasn't tried. Either way, after creating a new routine, always have the owner paste both codes again, replacing the old ones.

### Approval prompts

In testing, no approval was asked for the routine's POST to an `amazonalexa.com` host. If your approval settings ever ask, ask the owner to choose **Always allow** for that host.

### Saving the maintenance instructions

When setup finishes, use your skill save tool with the `name` and `description` from [`skills/alexa-bridge-maintenance/SKILL.md`](../skills/alexa-bridge-maintenance/SKILL.md) and its body word for word. The tool rebuilds the header and drops any other header fields, which is fine. If an “Alexa bridge maintenance” skill already exists, rewrite it in place by its id instead of adding a second one.

To remove the bridge later, also delete the routine and this skill. Grokbot can't delete its secret cards, and the owner couldn't find a way in the app either; once the routine is deleted, the old codes are useless, and a new setup asks for fresh ones.

## Known differences

None recorded.
