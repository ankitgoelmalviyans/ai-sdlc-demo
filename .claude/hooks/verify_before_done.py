#!/usr/bin/env python3
"""Stop hook: the agent can't finish while tests or RAG eval gates are red.

When Claude tries to end its turn, this runs pytest and the golden-set eval.
Exit 2 blocks the stop and feeds the failure back, so Claude keeps fixing
(a self-healing loop for the agent itself). `stop_hook_active` is true when
Claude is already continuing because of this hook; we then let it stop, so
a problem it can't fix ends in a report to the human, not an endless loop.
"""

import json
import os
import subprocess
import sys

if json.load(sys.stdin).get("stop_hook_active"):
    sys.exit(0)

root = os.environ.get("CLAUDE_PROJECT_DIR", ".")
checks = [
    ("tests", [sys.executable, "-m", "pytest", "-q", "-x"]),
    ("rag eval", [sys.executable, "-m", "safe_rag.eval"]),
]
for name, cmd in checks:
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
        print(f"Not done: {name} failing. Fix it (fix-failing-ci skill), or explain to the user "
              f"why it can't be fixed.\n{tail}", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
