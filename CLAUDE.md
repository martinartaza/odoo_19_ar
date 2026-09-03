# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this repository is

An **Odoo 19.0** tree currently used as the **laboratory for a Trello integration**. The
goal of this project is no longer the Magento connector.

What is being built here, one phase at a time, is a pipeline where a ticket written in
natural language on Trello ends in code, tests, a PR and a staging deploy. The pipeline is
the excuse: the declared priority is to **exercise Claude Code's capabilities** (skills,
hooks, MCP, subagents, webhooks, polling, permissions, sessions) over the result being
productive. Hence the deliberate redundancy — the same event is received by webhook *and*
by poll, so that idempotency has to be solved.

The Magento connector becomes **the material the pipeline works on**, not the goal. The
project's earlier documentation is archived in `CLAUDE_INTEGRATION.md`.

## The hard rule: never touch the published module's repo

`custom_addons/artaza_magento_connect` is published on the Odoo App Store from
`git@github.com:martinartaza/odoo_magento_connector.git`, branch `19.0`.

**Every git command on this machine targets `git@github.com:martinartaza/odoo_19_ar.git`
and only that one.** Never `odoo_magento_connector`: no push, no pull, no fetch, no
remote. That repo is the source of truth for what is published, and is maintained by hand,
outside this laboratory.

## The module here is an experimental copy

The copy in `custom_addons/artaza_magento_connect` starts from the published release
`19.0.1.0.0` and **diverges from here on**. The pipeline may modify it, break it and
rewrite it freely: it is not the source of truth for anything on the App Store, and no
real customer runs this code.

Running Odoo, in Docker:

```bash
docker compose up --build      # odoo (:8069) + postgres (:5434 on the host)
docker compose logs -f odoo
```

`entrypoint.sh` generates `/etc/odoo.conf` at start from `.env`, with
`addons_path = /opt/odoo/addons,/opt/odoo/custom_addons`.

The module's tests (195 tests; Magento is always mocked at the client boundary, so the
suite needs no reachable store and opens no socket):

```bash
./odoo-bin -d <testdb> -i artaza_magento_connect --test-enable \
           --test-tags /artaza_magento_connect --stop-after-init
```

A change under `static/` (JS or SCSS) is not visible until the module is reinstalled: the
deploy has to run `-u artaza_magento_connect`; restarting the server is not enough.

Linting with `ruff check .` (config in `ruff.toml`, auto-generated — do not hand-edit).

## Language

Everything is written in **English** — code, comments, documentation, commit messages,
branch names and Trello cards. Only the working conversation is in Spanish.

## Comments

A comment goes **above a function, not inside it**. A well-named function with its reason
stated above needs no narration of its steps, and a comment in the middle of a body is
almost always covering for a function that does too much or a name chosen badly — fix the
name or split the function instead.

One exception, because they are genuinely hard to read: a **regular expression or a
lambda**. There the comment gives **an example of input and output**, not an explanation
of the syntax:

```python
# "model: 3 opus" -> 3   ·   "modelo 3" -> no match
MODEL_LABEL = re.compile(r"^\s*model\s*:\s*(\d+)\b", re.I)
```

**No comments in XML, CSS or SCSS.** Odoo's views read as themselves, and a comment beside
a field or a selector ages worse there than anywhere else: the thing changes, the comment
does not, and the next reader believes the comment.

**A comment must not be longer than what it explains.** Eight lines of prose above three
lines of CSS is not thoroughness, it is reasoning in the wrong place. Why a selector had
to out-specify core, why an approach was chosen over another, what was rejected — that is
what the commit message and the pull request body are for. They carry it without ageing
into a lie every time the file is edited.

None of this applies to commit messages or to a pull request's body, which exist precisely
to say why.

## Security floor

These five rules apply from minute one and are not relaxed. They exist against
**accidents** before attackers: an ambiguous ticket like "clean up the test data" is
enough to lose the working tree without anyone attacking anything.

1. No destructive database commands (`dropdb`, `DROP DATABASE`, removing Docker volumes).
2. No writes outside `custom_addons/`. The pipeline's code lives in another repository
   (`ticket-agent`, a sibling of this tree) and is not touched from here: an agent does
   not rewrite its own guardrails.
3. Never read or expose `.env`, `clave.txt` or any secret, and never put them in a commit,
   a Trello comment or a published page.
4. No `git push` that does not go through a PR.
5. No writes to `.claude/**` or to this file: an agent does not edit the rules that
   govern it.

Written here they are **advice**. Once configured as `deny` rules in
`.claude/settings.json` they become **law**, which is the only thing that actually
protects. Both go in; neither replaces the other.

The production dumps at the root (`odoo-prod-*.gz`, `from_server/`, `filestore-odoo/`) are
in `.gitignore` and stay there: they are real data and `odoo_19_ar` is a **public** repo.

## Scope of work

Work advances **one phase at a time**, and the design is settled before code is written.
The pipeline specification — phases, branch model, mechanics — lives in the control-plane
repository, at `ticket-agent/docs/spec.md`. It does not belong in this file, which is
loaded into every session's context and has to stay short.

This repository tracks Odoo upstream 19.0. Local changes belong in `custom_addons/` and in
the deployment layer (`Dockerfile`, `entrypoint.sh`, `docker-compose.yml`, `config/`);
avoid touching `addons/` and `odoo/`.
