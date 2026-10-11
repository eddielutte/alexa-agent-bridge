# Alexa bridge maintenance: instructions for your agent

These are the steps behind the [`alexa-bridge-maintenance` skill](skills/alexa-bridge-maintenance/SKILL.md), which the agent saves when setup finishes. The [setup ground rules](SETUP.md#ground-rules-read-before-doing-anything) still apply: never type the owner's credentials, never put secrets in chat, and stop when something changes what the owner agreed to. Speak plainly to the owner (rule 8).

Work from the bridge checkout: the folder recorded as `checkout` in `~/.alexa-bridge/state.json`, which contains `bridge`. If your computer has been updated or reset, run `python3 -m bridge preflight` and then `python3 -m bridge dev-auth` first: they reinstall ASK CLI and, if the developer sign-in was lost, open it in your browser for the owner (as in setup).

## “Alexa says sign-in needs attention”

The owner hears this when Amazon has withdrawn the skill's sign-in. Their Echo links and routine are kept.

1. Run `python3 -m bridge test --status` and tell the owner what the skill reports.
2. If it still needs sign-in, make sure the routine's URL and key are available again as `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`: they're still in your secret store, and `python3 -m bridge webhook-check` confirms it. Without a store, ask the owner to run `python3 -m bridge webhook-set` again.
3. Repeat the speech sign-in, as in setup step 3, stop 1:
   1. Run `python3 -m bridge signin start`, open its `signin_url` and hand control to the owner (your profile's **Browser handover**).
   2. When Amazon shows “Sorry, we couldn't find that page”, take control back and copy the address bar to the clipboard.
   3. Run `python3 -m bridge signin finish --from-clipboard`, or `--from-file` with a private file. If it stops with `signin_check_failed`, run `python3 -m bridge signin check` a minute later instead of signing in again.
   4. Run `python3 -m bridge enrol`. From v0.1.7, the skill then tries to remove the replaced sign-in's entry from the owner's Amazon devices list by itself.
4. Run `python3 -m bridge test` and ask the owner to confirm they heard the test sentence.

## “Check the bridge”

1. Run `python3 -m bridge status` and `python3 -m bridge doctor --network`. `status` shows the installed release. If it shows `deploy_pending`, an update was cut off before its deploy finished: run `python3 -m bridge deploy`.
2. Run `python3 -m bridge test --status`. It asks the skill for its latest delivery status, which is the same as the owner saying “check cloud status”.
3. Report the results in plain words. “Amazon accepted” is not the same as “heard”.

## “Update the bridge”

1. Run `python3 -m bridge update --latest`. It finds the newest release, and says `up_to_date` if there's nothing newer. Otherwise it checks the release's signature, checks it out and redeploys. It refuses unsigned or unknown releases and a checkout with local changes, and it keeps the owner's links and sign-in. To install a particular release instead, use `--tag <release>`.
   - Then run `python3 -m bridge routine-text`. If it differs from your routine's instructions, replace them with it exactly. From v0.1.5 it tells the routine to ignore setup checks.
2. Ask the owner to try one short question afterwards.

## “Remove the bridge”

1. Confirm with the owner first; this cannot be undone.
2. Run `python3 -m bridge uninstall --yes`.
   - It first tries to remove the bridge's own entries from the owner's Amazon devices list: the skill's current sign-in (only the skill holds it), and any sign-in made on your computer but not yet sent to the skill.
   - Then it deletes the skill and `~/.alexa-bridge`, including the bridge's own ASK CLI.
   - The `uninstalled` event's `device_entry` says how that went. `removed` or `none` means Amazon accepted every removal, or there was nothing to remove. Anything else means an entry may still be there, and `next` says which one the owner should remove.
3. Remove the ASK CLI's developer sign-in: delete the `alexa-bridge` profile from `~/.ask/cli_config`, or delete `~/.ask` if nothing else on your computer uses ASK CLI.
4. Delete your “Alexa bridge” routine, the `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY` secrets, and the saved maintenance skill. If you can't delete stored secrets, tell the owner; once the routine is deleted, they're no longer usable.
5. If `device_entry` wasn't `removed`, or older entries are left from earlier installs or abandoned sign-ins, tell the owner where they are: Manage Content and Devices → Devices → Alexa. They're named like “<first name>'s <skill name> bridge (10 Oct 2026, 09:41 UTC)”, with the date and time of that sign-in. Entries from releases before v0.1.4 are named “<first name>'s AioAmazonDevices” (or “…2nd AioAmazonDevices”). The bridge can only remove the entry for its current sign-in. While the bridge is installed, keep the newest entry for the skill: it's the one the skill uses now, and removing it stops answers being spoken until the owner signs in again.

## Answering questions through the routine

Your “Alexa bridge” routine handles each spoken request on its own, so you don't need this skill for that. If the owner says the “Alexa request received” and “Alexa response sent” notes have stopped appearing in your conversation, save the standing rule from your profile's **Showing Alexa requests** section again. If the owner reports wrong-Echo answers, ask them to say “Alexa, ask <name> to link this Echo” on the requesting Echo and give its exact name.
