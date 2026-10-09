# Contributing

## Country results

The most useful contribution is a country report. After setting up the bridge, open an issue with the **Country report** template:

- the country and language
- whether the connection test and a real question were heard
- anything that looked different from the guide (copy entries from `~/.alexa-bridge/country-notes.json`)

Don't include names, email addresses, device serials, skill IDs or anything from `~/.alexa-bridge/private/`.

## Agent results

The bridge is tested with Grokbot. If you try another agent, open an issue with the **Agent report** template:

- the agent and plan you used
- which [requirements](AGENT-REQUIREMENTS.md) it met, and how
- where setup stopped or needed a workaround

A report that reaches a heard answer can become a profile in `agents/`, following [the profile guide](agents/README.md).

## Language packs

Follow [LANGUAGE-PACKS.md](LANGUAGE-PACKS.md). Packs start as `"status": "untested"` and become `proven` after a native speaker has checked the phrases and a real request has worked.

## Code

- Everything in `lambda/` must run on Python 3.8 with only the standard library and the runtime's boto3.
- Run `python3 -m unittest discover -s tests` and `python3 tools/check_secrets.py` before opening a pull request.
- Keep the delivery rules: never replay an uncertain send, never let a callback choose its Echo, and never log payloads, tokens, URLs or cookies.

## Releases

Releases are tags signed with the maintainer's SSH signing key:

- Key fingerprint: `SHA256:jFE7AQS44EuMt6TVafQICt2UtpwoO9iQCFSQQ0q/j9k` (ED25519)
- The public key is in [`allowed_signers`](allowed_signers) and is listed as a signing key on the maintainer's GitHub account (https://api.github.com/users/eddielutte/ssh_signing_keys).
- To verify a release yourself: `git -c gpg.ssh.allowedSignersFile=allowed_signers verify-tag vX.Y.Z`.

The setup guide has agents verify the tag after cloning. `bridge update` verifies each new tag against the `allowed_signers` file of the release already installed, so a changed key can't vouch for itself, and it refuses unsigned or wrongly signed tags.

Maintainers sign with `git tag -s vX.Y.Z -m vX.Y.Z`, with git set to `gpg.format=ssh` and `user.signingkey` pointing to the signing key's public file. Rotating the key means a release signed with the old key that adds the new one to `allowed_signers`.
