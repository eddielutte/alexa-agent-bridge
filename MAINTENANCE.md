# Alexa bridge maintenance: instructions for your agent

These are the steps behind the [`alexa-bridge-maintenance` skill](skills/alexa-bridge-maintenance/SKILL.md), which the agent saves when setup finishes. The [setup ground rules](SETUP.md#ground-rules-read-before-doing-anything) still apply: never type the owner's credentials, never put secrets in chat, and stop when something changes what the owner agreed to.

Work from the bridge checkout: the folder recorded as `checkout` in `~/.alexa-bridge/state.json`, which contains `bridge`. If your computer has been reset, run `python3 -m bridge preflight` and then `python3 -m bridge dev-auth` first. Setup progress is kept in `~/.alexa-bridge/state.json`.

## “Alexa says sign-in needs attention”

The owner hears this when Amazon has withdrawn the skill's sign-in. Their Echo links and routine are kept.

1. Run `python3 -m bridge test --status` and tell the owner what the skill reports.
2. If it still needs sign-in, first make sure the routine's URL and key are available again as `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY`. They're still in your secret store; without one, ask the owner to run `python3 -m bridge webhook-set` again.
3. Then repeat setup step 5:
   1. `signin start`
   2. hand control to the owner
   3. save the maplanding address to a private file
   4. `signin finish --from-file …` (if it stops with `signin_check_failed`, run `signin check` a minute later instead of signing in again)
   5. `enrol`
4. Run `python3 -m bridge test` and ask the owner to confirm they heard the test sentence.

## “Check the bridge”

1. Run `python3 -m bridge status` and `python3 -m bridge doctor`.
2. Run `python3 -m bridge test --status`. It asks the skill for its latest delivery status, which is the same as the owner saying “check cloud status”.
3. Report the results in plain words. “Amazon accepted” is not the same as “heard”.

## “Update the bridge”

1. Find the newest release tag without downloading any tags: `git ls-remote --tags --refs origin 'v*'` in the checkout, and confirm the tag with the owner.
2. Run `python3 -m bridge update --tag <tag>`. It refuses unsigned or unknown releases, anything not newer than the installed release, and a checkout with local changes. It keeps the owner's links and sign-in.
3. Ask the owner to try one short question afterwards.

## “Remove the bridge”

1. Confirm with the owner first; this cannot be undone.
2. Run `python3 -m bridge uninstall --yes`.
3. Remove the ASK CLI's developer sign-in for setup: delete the `alexa-bridge` profile from `~/.ask/cli_config`, or delete `~/.ask` if nothing else on your computer uses ASK CLI.
4. Delete your “Alexa bridge” routine and the `BRIDGE_WEBHOOK_URL` and `BRIDGE_WEBHOOK_KEY` secrets, and the saved maintenance skill.
5. Tell the owner they can remove the leftover device registration, named like “<first name>'s AioAmazonDevices”, from their Amazon devices list. If they run more than one bridge, check its date before removing it.

## Answering questions through the routine

Your “Alexa bridge” routine handles each spoken request on its own, so you don't need this skill for that. If the owner reports wrong-Echo answers, ask them to say “Alexa, ask <name> to link this Echo” on the requesting Echo and give its exact name.
