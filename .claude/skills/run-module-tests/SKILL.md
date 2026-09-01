---
name: run-module-tests
description: Run the artaza_magento_connect unit test suite against a disposable Odoo database. Use whenever a change to the module has to be verified, before opening a PR, or when a ticket's acceptance criteria mention the tests still passing.
---

# Running the module's tests

```bash
.claude/skills/run-module-tests/run-tests.sh
```

That is the whole thing. It prints a verdict line and writes the full log to
`/tmp/odoo-tests-artaza_magento_connect.log`. A passing run looks like:

```
verdict  : 0 failed, 0 error(s) of 195 tests when loading database 'odoo_test'
```

Exit code is 0 on success, 1 on test failure or a crash, 2 when the script refuses to run.

To narrow the run while iterating, pass test tags:

```bash
.claude/skills/run-module-tests/run-tests.sh /artaza_magento_connect:TestTaxMatch
```

## Do not run the command from the module's README

`README.md` documents:

```bash
./odoo-bin -d <testdb> -i artaza_magento_connect --test-enable \
           --test-tags /artaza_magento_connect --stop-after-init
```

That is correct for a bare checkout and **fails in this environment**. Three things had to
be worked out, and none of the errors point at the real cause:

**1. The port is taken.** Odoo already runs inside the container, so a second `odoo-bin`
cannot bind 8069 and dies with `Address already in use` — a message that says nothing
about tests. `--no-http` does *not* fix it: it only sets `http_enable=False`, while
`service/server.py` builds the `ThreadedServer` and binds the socket regardless. The fix
is `--http-port=8199`, an unused port.

**2. Crons fire during the run.** The module ships scheduled jobs. `--max-cron-threads=0`
keeps them out of the test transaction.

**3. `odoo-bin` is not on the host.** Odoo lives in the `odoo19` container; the command
has to run through `docker exec`.

## The false green

`-i` only does something when the module is **not yet installed**. Run the README command
twice against the same database and the second run installs nothing, executes no tests,
and reports:

```
0 failed, 0 error(s) of 0 tests
```

Which reads as a pass. A pipeline trusting that line would merge a change whose tests
never ran — the worst possible failure mode, because it is silent and looks like success.

The script defends on both sides: it asks the database whether the module is installed and
picks `-i` or `-u` accordingly, and it treats *any* run of 0 tests as a failure. Note the
state query goes to the **database** container: `psql` does not exist in the Odoo one.

## The safety rule this script exists for

The suite runs with **`-i`, which reinstalls the module**. Against a database in use, that
destroys its configuration and data — the Magento URL and token included.

So the script refuses to touch `odoo`, `postgres`, `prueba_store` or `ticket-ai`, and
defaults to the disposable `odoo_test`. Override with `TEST_DB=...` only for another
throwaway database.

This is exactly the kind of rule that belongs in a script rather than in a comment: a
warning can be skimmed, a guard cannot.

## Reading the result

Do **not** read the whole log — it is around 1100 lines of per-test INFO chatter and
carries almost no information. The script already extracts what matters: the verdict, and
on failure the failing assertions. Open the log only when that is not enough.

## What this does not cover

Unit tests only. Magento is mocked at the client boundary, so the suite needs no reachable
store and opens no socket. It says nothing about whether a change is visible in the
browser — that is integration testing, and it is not built yet.

Anything under `static/` is invisible until the module is upgraded (`-u`), and this script
uses `-i` on a separate database, so a passing suite does **not** mean the asset change
works in the running instance.
