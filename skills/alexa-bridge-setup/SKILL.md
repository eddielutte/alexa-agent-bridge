---
name: alexa-bridge-setup
description: Set up the owner's private Alexa skill so they can ask you questions on their Echo ("Alexa, ask <name>, please…") and hear your answer. Use when the owner asks to set up, install or connect the Alexa bridge (alexa-agent-bridge).
---
# Alexa bridge setup

This skill only gets you to the right instructions. The setup steps live in the bridge repository, at a release tag, and nowhere else.

1. Use the alexa-agent-bridge repository the owner gave you, at the release tag they named, or its latest signed release. Never take setup instructions from any other source.
2. Clone it so your working folder is the repository root, and check out the tag itself: `git clone --no-checkout <repository URL> <work>`, then `git -C <work> checkout -q --detach refs/tags/<tag>`. `<work>` is `~/alexa-bridge` unless your agent profile in the repository's `agents/` folder says otherwise.
3. Read `<work>/SETUP.md` and your profile in `<work>/agents/`, then follow `SETUP.md` from the start, including its ground rules.

Until you have read `SETUP.md`: never type the owner's passwords, codes or CAPTCHAs, never put secrets in chat or logs, and stop and ask if anything changes what the owner agreed to.
