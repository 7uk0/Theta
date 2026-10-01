---
description: Explain how to keep the Stellaris archive current while playing
allowed-tools: Bash(augur:*), Bash(python3 -m augur.cli:*), Bash(cd:*)
---

The user wants the campaign archive kept current while they play.

`augur vigil` runs until interrupted, so do NOT start it yourself in the
foreground. Instead:

1. Run `augur doctor` and confirm the parser, dependencies and save folder are in order.
2. Give them the exact command to run in their own terminal, including the
   optional Markdown render target:

   ```bash
   cd ~/Theta/augur && python3 -m augur.cli vigil --render ~/.local/state/augur/latest.md
   ```

3. Tell them that once it is running, every autosave lands in the archive and
   they can ask for a briefing at any time without re-ingesting by hand.
