#!/usr/bin/env python3
"""Stop: refuse to finish with Python changed and the suite not run since.

Two jobs, and only one of them needed the runner.

1. The gate. If a .py under custom_addons/ is newer than the last test log, the
   suite has not been run since that edit, and this exits 2 -- which does not
   remind the model, it stops it from finishing. A green suite is the module's
   only real evidence, and "I'll run them next time" is exactly what a Stop hook
   exists to prevent.

2. The report. When TICKET_CARD_ID is set, the outcome is appended to a summary
   file the runner reads and publishes to the card. The hook deliberately does
   NOT call Trello: it holds no credentials and needs none. It writes a fact;
   something else publishes it. Same split as the deploy -- trigger here,
   authority elsewhere.

Exit codes (Stop contract):
  0  may finish
  2  may not; stderr is handed to the model as its next instruction
`stop_hook_active` is honoured first: without that check a hook that always
refuses would loop forever.
"""
import json
import os
import subprocess
import sys
import time

MODULE = "artaza_magento_connect"
SRC = f"custom_addons/{MODULE}"
TEST_LOG = os.environ.get("TEST_LOG", f"/tmp/odoo-tests-{MODULE}.log")
SUMMARY = "/tmp/claude-run-summary.jsonl"


def changed_python(project_dir):
    """Uncommitted .py files under the module -- the work of this session."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", SRC],
        capture_output=True, text=True, cwd=project_dir, timeout=30,
    )
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip().split(" -> ")[-1]
        if path.endswith(".py"):
            out.append(path)
    return out


try:
    event = json.load(sys.stdin)

    # First, always: a hook that refuses unconditionally would never let the
    # model finish. When it is re-entered after having blocked once, it lets go.
    if event.get("stop_hook_active"):
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    card = os.environ.get("TICKET_CARD_ID")

    changed = changed_python(project_dir)
    log_mtime = os.path.getmtime(TEST_LOG) if os.path.exists(TEST_LOG) else 0
    stale = [
        p for p in changed
        if os.path.exists(os.path.join(project_dir, p))
        and os.path.getmtime(os.path.join(project_dir, p)) > log_mtime
    ]

    if card:
        with open(SUMMARY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "card_id": card,
                "session_id": event.get("session_id"),
                "changed_python": changed,
                "tests_stale": bool(stale),
            }) + "\n")

    if stale:
        print(
            "Not finished yet: "
            + ", ".join(stale[:5])
            + (" and others" if len(stale) > 5 else "")
            + " changed since the test suite last ran"
            + (f" ({time.strftime('%H:%M:%S', time.localtime(log_mtime))})" if log_mtime else ", which has never run here")
            + ".\n\nRun the suite before finishing:\n"
            "    ./.claude/skills/run-module-tests/run-tests.sh\n\n"
            "It takes about a minute and prints one line. If it cannot run "
            "(the container is down, say), say so plainly in your reply "
            "instead of leaving the change unverified.",
            file=sys.stderr,
        )
        sys.exit(2)

except Exception as exc:  # noqa: BLE001 - a broken gate must not trap the session
    print(f"stop-gate error (ignored): {exc}", file=sys.stderr)

sys.exit(0)
