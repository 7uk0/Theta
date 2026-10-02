---
description: Explain how to keep the Stellaris archive current while playing
allowed-tools: Bash(augur:*), Bash(python3 -m augur.cli:*), Bash(cd:*)
---

The user wants the campaign archive kept current while they play.

`augur vigil` runs until interrupted, so do NOT start it yourself in the
foreground. Instead:

1. Run `augur doctor` and confirm the parser, dependencies and save folder are in order.
2. Give them the dashboard command to run in their own terminal — it watches,
   ingests, and shows status, resources, trends and events as they play:

   ```bash
   augur dash
   ```

   Keys: `r` re-read now, `j`/`k` scroll events, `q` quit.

3. If they would rather have no UI, give them the headless form instead:

   ```bash
   augur vigil --render ~/.local/state/augur/latest.md
   ```

4. Tell them that once either is running, every autosave lands in the archive
   and they can ask for a briefing at any time without re-ingesting by hand.
