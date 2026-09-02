#!/usr/bin/env python3
"""PreToolUse guard, in the two directions a deny list cannot cover.

Permission rules match a command *prefix*, so `docker exec odoo-db-1 pg_dump ...`
walks straight past `Bash(pg_dump:*)` -- and `docker exec` cannot itself be
denied, because the test skill runs through it. Only code can read the whole
command line. That is this hook's first job.

Its second job points the other way. Reading a file and writing to a Trello card
are both ordinary work; the harm is in doing them in sequence. `ask` cannot gate
the outbound side either, because a headless run has nobody to ask. So the text
about to be published gets inspected before it leaves.

Exit codes (PreToolUse contract):
  0  allowed
  2  blocked, and stderr explains why to the model
Any internal failure exits 0. A guard that crashes must not become a lock: the
deny rules in settings.json are the layer that holds regardless of this file.
"""
import json
import re
import sys

# --- Direction 1: the whole Bash command line -------------------------------

# Split a command into segments so `ls x && cat secret` is not judged by its
# first word alone. Substitutions ($(...), backticks) are NOT unwrapped -- see
# "Known limits" at the bottom.
SEPARATORS = re.compile(r"\|\||&&|[;|\n]")

# Commands that only ever look at metadata. Naming a protected path from one of
# these is allowed: `ls -la odoo-prod-*.gz` reveals a size, not a record.
METADATA_ONLY = re.compile(r"^\s*(?:sudo\s+)?(?:ls|du|stat|file|find|wc|basename|dirname)\b")

# Segments that emit text rather than read files. Naming a protected path inside
# a commit message or a heredoc is *talking about* the rule, not breaking it --
# and the first thing this hook did on being switched on was block the commit
# that introduced it. Only an embedded secret *value* still disqualifies these.
WRITES_TEXT = re.compile(
    r"^\s*(?:git\s+(?:commit|tag)|echo|printf)\b"
    r"|^\s*(?:sudo\s+)?(?:cat|tee)\b[^|]*<<"  # a heredoc writes; it does not read
)

SECRET_PATHS = [
    (r"(?<![\w.])\.env(?!\.(?:example|sample|template|dist))\b", "reads or writes .env"),
    (r"\bclave\.txt\b", "reads clave.txt"),
    (r"\bprintenv\b", "dumps the environment, which carries the secrets"),
    (r"docker\s+compose\s+config\b", "renders docker-compose with .env already substituted"),
]

SECRET_VALUES = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "carries a private key inline"),
    (r"\b\w*(?:secret|token|passwd|password)\w*\s*=\s*['\"]?\S{8,}", "assigns a credential inline"),
]

DUMPS = [
    (r"\bpg_dump(?:all)?\b", "creates a database dump"),
    (r"\bmysqldump\b", "creates a database dump"),
    (r"\bpg_restore\b", "restores a database dump"),
    (r"/web/database/(?:backup|dump)", "asks Odoo for a database backup over HTTP"),
]

PROD_DATA = [
    (r"odoo-prod-", "reads the production dump"),
    (r"(?<![\w-])from_server/", "reads the production files pulled from the server"),
    (r"(?<![\w-])filestore-odoo/", "reads the production filestore"),
]

DESTRUCTIVE = [
    (r"\bDROP\s+(?:DATABASE|SCHEMA|TABLE)\b", "drops a database object"),
    (r"\bTRUNCATE\b", "truncates a table"),
    (r"\bdropdb\b", "drops a database"),
    (r"docker\s+(?:volume\s+(?:rm|prune)|compose\s+down\s+.*-v)\b", "removes a Docker volume"),
]


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def split_heredocs(cmd):
    """Separate `cmd` into (commands, data).

    Segments are split on newlines, so without this the body of a heredoc
    arrives as a series of orphan lines and gets judged as if each were a
    command. Bodies belonging to a text-writing opener are dropped -- that
    covers `cat > f <<EOF` and the `git commit -m "$(cat <<EOF" ` form a commit
    message takes. `bash <<EOF` is code, not data, and stays under inspection,
    as does anything piped onward.
    """
    lines = cmd.split("\n")
    commands, data, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        commands.append(line)
        opener = HEREDOC.search(line)
        # Test the sub-command that actually opens the heredoc, not the start of
        # the line: `git add x && git commit -m "$(cat <<EOF` opens it from the
        # second half. A pipe anywhere disqualifies the line -- `cat <<EOF | bash`
        # is code however the body is written.
        head = SEPARATORS.split(line)[-1] if "|" not in line else line
        if opener and "|" not in line and WRITES_TEXT.search(head):
            marker = opener.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != marker:
                data.append(lines[i])
                i += 1
        i += 1
    return "\n".join(commands), "\n".join(data)


def scan_command(cmd):
    """(reason, matched text) for the first thing that disqualifies `cmd`."""
    commands, data = split_heredocs(cmd)

    # The body of a heredoc is text, not a command -- but text still carries a
    # secret perfectly well, so it is checked for values even though naming a
    # protected path in it is fine.
    for pattern, reason in SECRET_VALUES:
        hit = re.search(pattern, data, re.IGNORECASE)
        if hit:
            return reason, hit.group(0).strip()

    for segment in SEPARATORS.split(commands):
        if WRITES_TEXT.search(segment):
            groups = [SECRET_VALUES]
        elif METADATA_ONLY.search(segment):
            # A path may be named; its contents may not be opened.
            groups = [SECRET_PATHS, SECRET_VALUES, DUMPS, DESTRUCTIVE]
        else:
            groups = [SECRET_PATHS, SECRET_VALUES, DUMPS, DESTRUCTIVE, PROD_DATA]
        for group in groups:
            for pattern, reason in group:
                hit = re.search(pattern, segment, re.IGNORECASE)
                if hit:
                    return reason, hit.group(0).strip()
    return None


# --- Direction 2: what is about to be published -----------------------------

PUBLISHES = re.compile(r"^mcp__trello__(add_comment|set_card_description|attach_image)$")

# A git sha is exactly 40 hex, and the card is *supposed* to carry one, so that
# one length is exempt. Trello's own key (32) and secret (64) are not.
SECRET_SHAPES = [
    (r"(?<![0-9a-f])([0-9a-f]{32,})(?![0-9a-f])", "a long hex string, the shape of an API key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key block"),
    (r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S{6,}", "a credential"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
]

LEAKED_PATHS = [
    (r"odoo-prod-\S+", "a path to the production dump"),
    (r"(?<![\w-])(?:from_server|filestore-odoo)/\S*", "a path inside the production data"),
]


def scan_payload(value):
    for pattern, reason in SECRET_SHAPES:
        for hit in re.finditer(pattern, value, re.IGNORECASE):
            text = hit.group(0)
            if len(hit.groups()) and hit.group(1) and len(hit.group(1)) == 40:
                continue  # a git commit sha
            return reason, text[:24] + ("..." if len(text) > 24 else "")
    for pattern, reason in LEAKED_PATHS:
        hit = re.search(pattern, value, re.IGNORECASE)
        if hit:
            return reason, hit.group(0)
    return None


def walk(value):
    """Every string anywhere in the tool's arguments."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def block(what, reason, evidence):
    print(
        f"BLOCKED by the PreToolUse guard.\n\n"
        f"  {what}\n"
        f"  reason:   {reason}\n"
        f"  matched:  {evidence}\n\n"
        f"This is the security floor, not a preference: production data, secrets and "
        f"destructive database commands are out of bounds whatever the ticket says. "
        f"Do not rephrase the command to get around it. If the ticket genuinely "
        f"requires this, stop and say so in your report -- a human decides.",
        file=sys.stderr,
    )
    sys.exit(2)


try:
    event = json.load(sys.stdin)
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}

    if tool == "Bash":
        found = scan_command(args.get("command") or "")
        if found:
            block("Bash command", *found)

    elif PUBLISHES.match(tool):
        for text in walk(args):
            found = scan_payload(text)
            if found:
                block(f"publishing through {tool}", *found)

except Exception as exc:  # noqa: BLE001 - a crashing guard must not become a lock
    print(f"guard hook error (ignored): {exc}", file=sys.stderr)

sys.exit(0)

# Known limits, so nobody trusts this further than it reaches:
#   - Only the command *line* is read, never the contents of what it runs. A
#     script written to a file and then executed passes unseen. Writing this
#     hook's own test harness ran straight into that: a harness whose arguments
#     are full of .env and pg_dump can no longer be typed at a prompt, so it had
#     to move into a file -- and from a file it runs unexamined. Closing that
#     would mean inspecting every file write, which would block this very file.
#   - Command substitution is not unwrapped: `cat $(echo .env)` is not seen.
#   - The outbound scan matches the *shape* of a secret. It does not, and cannot,
#     detect a prose summary of customer data. That threat needs a human reading
#     the card, which is what the approval step in the pipeline is for.
#   - Everything here runs inside the same process as the model. The layers that
#     hold when this one does not are the container and the pull request.
