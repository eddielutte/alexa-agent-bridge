---
name: Agent report
about: Tell us how the bridge worked with an agent other than Grokbot
title: "Agent report: <agent>"
labels: agent-report
---

**Agent and plan:** <!-- e.g. the product name and subscription tier -->

**Release tag used:** <!-- e.g. v1.0.0 -->

**Requirements** (see AGENT-REQUIREMENTS.md; say how the agent met each one, or why it couldn't)
- [ ] Answering 1: authenticated HTTPS webhook
- [ ] Answering 2: a 2xx reply (which status?) within 4 seconds
- [ ] Answering 3: runs unattended
- [ ] Answering 4–5: one callback within 120 seconds, tokens kept private
- [ ] Setup 1: a shell with Python, Node and git
- [ ] Setup 2: a browser it can hand to the owner
- [ ] Setup 3: a secure secret store (or the `webhook-set` fallback)
- [ ] Setup 4: saved instructions or skills

**Results**
- [ ] Setup finished without a failed step
- [ ] The connection test sentence was heard in full
- [ ] A real question was answered on the same Echo

**Where setup stopped or needed a workaround:**

<!-- Never include names, email addresses, device serials, skill IDs, webhook URLs or keys, tokens or anything from ~/.alexa-bridge/private/. -->
