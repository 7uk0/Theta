"""augur — read the omens in a Stellaris save.

Parses a Stellaris ``.sav``, tracks what changed between saves, and hands the
result to whatever is asking: a shell, a file, or Claude Code. There is no
model client in this package. The briefing is the product; the reasoning over
it happens wherever you run ``augur``.
"""

__version__ = "0.1.0"
