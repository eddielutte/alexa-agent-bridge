# Grokbot

**Status:** tested end to end in the United Kingdom. A Grokbot set up a bridge from the repository link, with the owner only signing in, choosing and listening, and spoken requests were answered on the owner's Echos.

## Capabilities

| Requirement | Grokbot feature | Notes |
| --- | --- | --- |
| Answering 1: authenticated HTTPS webhook | A routine with a **webhook trigger** | Grokbot generates the URL and key. It can't read them itself; the owner copies them into secret cards |
| Answering 2: a 2xx reply within 4 seconds | The trigger acknowledges with 200 and runs the routine afterwards | Proven. A first request after a deploy has taken more than 2 seconds, so the 4-second limit matters |
| Answering 3: runs unattended | Routine runs | No approval was asked for the callback to `amazonalexa.com` |
| Answering 4–5: one callback, tokens kept private | Followed from the routine instructions | Proven |
| Setup 1: shell | The bot's own computer: Debian Linux (x86-64) with Python 3, Node, npm, git and `libX11`. No root, no `ssh-keygen`, no tkinter and no clipboard tool | The bridge needs nothing more: it installs ASK CLI under `~/.alexa-bridge/tools` and reads the clipboard itself. Files survive between sessions. “Update Bot's Computer” moves the bot to a fresh instance that keeps files and logins (so the developer sign-in in `~/.ask` survives) but drops installed packages; `bridge setup` reinstalls ASK CLI. “Reset” restores the last saved snapshot and may lose recent work, and its exact extent is unknown (Grokbot, 11 October 2026) |
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

- **The speech sign-in.** Open the `signin_url` yourself. Amazon's last page says “Sorry, we couldn't find that page”, with a dog; tell the owner in advance that this means it worked. That page is where Amazon always returns this sign-in, with the one-time code in its address; the Alexa phone app catches it unseen, so in a browser Amazon has no page to show. As soon as the owner hands back, your browser helper copies the address bar (Ctrl+L, Ctrl+A, Ctrl+C in Chrome). Then run `python3 -m bridge setup` straight away, because Amazon's code expires within minutes. The bridge reads the clipboard itself, on every display, so no clipboard tool is needed. Never paste the address into chat.
- **Opening these long addresses:** give your helper the exact address and tell it to use its **navigate** tool. On 11 October 2026 its browser script tool rejected a long sign-in address twice, and navigate then opened it first time. Grokbot knows of no length limit; dog-page addresses of about 1,150 characters have been copied without trouble.
- **Check the page yourself before skipping the hand-over or copying.** On 11 October 2026 the helper reported that the dog page was already showing straight after opening the sign-in page; Grokbot's own screenshot showed Amazon's sign-in form. Hand control to the owner unless your own screenshot shows the dog page, and copy only after they hand back. The address bar can still show an earlier sign-in's dog-page address, open and highlighted, which is likely what misled the helper; the page itself is what counts (seen in the v0.1.9 retest). Delete any screenshots that show the address bar once you're done.
- **If your helper won't copy the address:** that is a safety refusal, not a length problem. On 11 October 2026 the helper declined to put the dog page's address into the clipboard because it carries a sign-in code, although the same copy had worked on an earlier install that day. When you ask it, say that this is the owner's own sign-in, made at their request, and that the address goes only to the bridge on this computer and is never shown. If it still declines, try once more, then ask the owner to copy it while the page is open, telling them exactly what to press: click the address bar, then Ctrl+A and Ctrl+C. Warn the owner before the hand-over that you may ask this.
- **Don't use the browser's debugging ports.** Grokbot's own rules forbid shell commands from driving the browser over Chrome DevTools: no attaching, no automation clients, no reading tabs or cookies. Every agent's Chrome on the computer exposes one, and they can see each other's tabs. The helper is the only route (checked 11 October 2026).
- **Tidy up afterwards.** Open each sign-in page in a new tab. Once the bridge has accepted the copied address, close that tab (the sign-in step's `phase_done` event says so), and close the developer sign-in's tab in the same way. Don't close a tab earlier: if a step stops, you may need to copy the address again. A used address is worthless, but other assistants on the computer can read open tabs, and a leftover address has misled the helper.
- **Sign the owner out when the browser steps are over:** after `enrol`, whose `phase_done` event prompts this (in setup and in a sign-in repair), and after a new developer sign-in. Use the account menu's plain **Sign out** on www.amazon.com, and on developer.amazon.com if your browser is signed in there; never an option that signs out other devices. Check your own screenshot that each site now offers “Sign in” before you close its tab; in the v0.1.12 retest the tabs were closed first, so only the helper's report confirmed the sign-out. The bridge keeps its own sign-ins, so it doesn't need the browser's, and other assistants on the computer can reach an open Amazon session. In the v0.1.11 retest (11 October 2026), signing out left the bridge's speech and developer sign-ins working. The next sign-in then needs the owner's password and code again. Don't clear the browser's history or cookies.
- **Finish with one blank tab.** After signing out, close every tab you opened for the bridge, including the Amazon pages you used to sign out or to check the developer site, and leave one blank new tab. Don't close the last tab: Chrome would close its window, and the next sign-in would start with no browser open. In the v0.1.11 retest (11 October 2026) the sign-in tabs were closed, but the sign-out and developer-site tabs were left open.
- **The developer sign-in.** `bridge setup` starts ASK CLI's sign-in in the background and prints `dev_signin_url`. Open that page yourself, as for the speech sign-in, and hand control; the owner signs in and clicks **Allow**. Amazon then shows a page whose address contains `ask-cli-no-browser.html`. Copy that address bar straight away and run `setup`; the bridge hands the code to ASK CLI. Proven on 11 October 2026: the copied address was on one of the computer's four displays, and the bridge's reader found it there. An Update keeps this sign-in, but a Reset may remove it; `setup` then asks for it again. You can't keep it in your secure store, because only the owner can save secret cards.

### Connecting the routine

1. Create the routine on yourself, named “<Display name> Alexa bridge”, with a **webhook trigger**, and paste the `routine-text` instructions exactly.
2. You can't read the routine's webhook URL and key yourself. Post the routine's two field links (Webhook URL, Webhook key) with a short explanation, such as “Two connection codes for the routine: click each link, copy, and paste into the matching card. I never see them.” Then post the two **secure secret cards**, `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`. A secret card ends your turn, so they come one after the other: the second when the first is saved (as on 11 October 2026).
3. The links and cards are the only route for the codes, so the owner doesn't need to look anywhere else in the app; if they ask, post the links again.
4. Run `python3 -m bridge setup`. It sends the routine a setup check and confirms only the URL's base domain and the key's length. In a proof of concept on 11 October 2026, the routine replied 200 to the right key and 401 to a wrong one, and the run did nothing visible.
5. **Reinstalling:** you can't delete secret cards, so the cards from an earlier install are still there and still loaded. On 10 October 2026, setup accepted these old codes and connected the skill to a deleted routine. From v0.1.5 the setup check catches this (`webhook_unreachable`). On 11 October 2026, Grokbot answered 401 both for a wrong key and for a webhook address with no routine, so the check can't tell the two apart; a **deleted** routine's own address wasn't tried. Either way, after creating a new routine, always have the owner paste both codes again, replacing the old ones.

### Approval prompts

Expect up to two approval prompts, and ask the owner to choose **Always allow** for each:
- **The routine's answer callback** (“Send Alexa answer callback”), its POST to an `amazonalexa.com` host. On 11 October 2026 it was held once by Grokbot's safety check, on the second question, and needed this approval; earlier installs weren't asked. Without “Always allow”, an unattended answer can be held, and the Echo stays silent.
- **The background helper** that copies a sign-in page's address (“Use the subagent tool to inspect local browser address bars”).

### Showing Alexa requests

A routine can't post into your chat with the owner. Each run's output, including its “Alexa request received” and “Alexa response sent” notes, comes back to you privately as a completion note. Earlier installs relayed these to the owner. On 11 October 2026, the notes weren't relayed, because nobody had asked about the runs, and the owner noticed they were missing. So, as part of the hand-over, save a standing rule to your memory:

> Whenever a “<Display name> Alexa bridge” run finishes and its note contains “Alexa request received”, post its “Alexa request received” and “Alexa response sent” sections into the owner's chat straight away, word for word, with nothing added, even if nobody asked. If a run reports a failure instead, post that briefly. Never include tokens, keys or callback addresses.

Confirm to the owner that the rule is saved. On 11 October 2026, Grokbot saved this rule and posted the two missed notes.

### Saving the maintenance instructions

When setup finishes, use your skill save tool with the `name` and `description` from [`skills/alexa-bridge-maintenance/SKILL.md`](../skills/alexa-bridge-maintenance/SKILL.md) and its body word for word. The tool rebuilds the header and drops any other header fields, which is fine. If an “Alexa bridge maintenance” skill already exists, rewrite it in place by its id instead of adding a second one.

To remove the bridge later, also delete the routine and this skill. Grokbot can't delete its secret cards, and the owner couldn't find a way in the app either; once the routine is deleted, the old codes are useless, and a new setup asks for fresh ones.

## Known differences

None recorded.
