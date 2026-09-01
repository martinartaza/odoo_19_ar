#!/usr/bin/env python3
"""PostToolUse quality gate: ruff, but only on what the edit introduced.

The module carries pre-existing violations (30 across 10 files at the time of
writing). A hook that failed on any violation would block the first edit to
sale_order.py over debt the agent did not write, and -- worse -- would push it
to "fix" files outside the ticket. So the baseline is the version in HEAD: this
reports a violation only when the edit raised the count for that rule in that
file. Debt already committed stays the human's call; debt added here does not.

Exit codes (PostToolUse contract):
  0  nothing new -- silent
  2  new violations -- stderr goes back to the model, which then fixes them
Anything that goes wrong inside the hook exits 0: a broken quality gate must
never be able to block work.
"""
import collections
import json
import os
import shutil
import subprocess
import sys

LINT_ROOT = "custom_addons/"  # Odoo core is off-limits, so never lint it.
MAX_LINES = 20


def violations(path, source=None):
    """Ruff's findings for `path`. `source`, when given, is linted in its place."""
    cmd = ["ruff", "check", "--output-format", "json"]
    cmd += [path] if source is None else ["--stdin-filename", path, "-"]
    proc = subprocess.run(
        cmd, input=source, capture_output=True, text=True, timeout=60, cwd=PROJECT_DIR,
    )
    if proc.returncode not in (0, 1):  # 2 = ruff itself failed (bad config, syntax)
        return None
    return json.loads(proc.stdout or "[]")


try:
    event = json.load(sys.stdin)
    PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    path = (event.get("tool_input") or {}).get("file_path") or ""
    rel = os.path.relpath(os.path.abspath(path), PROJECT_DIR)

    if not rel.endswith(".py") or not rel.startswith(LINT_ROOT):
        sys.exit(0)
    if not shutil.which("ruff"):
        print("ruff is not installed: quality gate skipped", file=sys.stderr)
        sys.exit(0)  # non-blocking; the user sees it, the model is not derailed

    found = violations(rel)
    if not found:
        sys.exit(0)

    # Baseline: the same file as committed. A file git does not know is all-new,
    # so every violation in it counts against the edit.
    head = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    base = violations(rel, head.stdout) if head.returncode == 0 else []
    if base is None:
        sys.exit(0)

    now_by_code = collections.Counter(v["code"] for v in found)
    base_by_code = collections.Counter(v["code"] for v in base)
    added = {c: n - base_by_code[c] for c, n in now_by_code.items() if n > base_by_code[c]}
    if not added:
        sys.exit(0)

    lines = []
    for v in found:
        if v["code"] not in added:
            continue
        # When the rule was absent from HEAD every hit is new; otherwise ruff
        # gives counts, not identities, so say which lines are candidates.
        mark = "" if base_by_code[v["code"]] == 0 else "  (this rule also fires elsewhere in HEAD)"
        lines.append(
            f"  {rel}:{v['location']['row']}:{v['location']['column']}: "
            f"{v['code']} {v['message']}{mark}"
        )

    print(
        f"ruff: this edit introduced {sum(added.values())} new violation(s) in {rel} "
        f"({', '.join(f'{c} x{n}' for c, n in sorted(added.items()))}):\n"
        + "\n".join(lines[:MAX_LINES])
        + "\n\nFix only these. Violations already present in HEAD are pre-existing "
          "debt and are out of scope -- do not touch them.",
        file=sys.stderr,
    )
    sys.exit(2)

except Exception as exc:  # noqa: BLE001 - a broken gate must not block work
    print(f"ruff hook error (ignored): {exc}", file=sys.stderr)
    sys.exit(0)
