---
description: Read or continue the Stellaris campaign Chronicle
argument-hint: [read | write | continue]
allowed-tools: Bash(augur:*), Bash(python3 -m augur.cli:*), Write(/tmp/**)
---

Use the `augur` skill to work on the campaign Chronicle.

Request: **$ARGUMENTS**

- Nothing, or "read" → `augur chronicle read` and present the existing chapters.
- "write", "continue", or anything asking for new prose → pull
  `augur chronicle source --json`, draft the next chapter in the empire's own
  voice, and show it. Do not save it until the user asks.

Follow the Chronicle write sequence in the skill: fresh read for the
`campaign_ref` and `chronicle_revision`, prose to a file, write only on an
explicit request to save.
