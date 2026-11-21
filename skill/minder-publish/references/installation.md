# Local CLI installation

Use `minder-publish` in the agent's actual execution environment. The managed
skill installer replaces this reference with that installation's absolute
executable path, so a fresh session does not depend on its shell's PATH.

If the executable is unavailable, use the selected instance's Connection
instructions to install the CLI and skill in that environment. Do not request a
device token in chat; the CLI accepts it through a hidden terminal prompt.
