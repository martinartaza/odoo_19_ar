#!/usr/bin/env bash
# Run the artaza_magento_connect test suite inside the running Odoo container.
#
# Prints a one-line verdict and keeps the full log on disk. The full run is
# ~1100 lines; pouring that into a session's context is a waste, and the only
# lines that matter are the failures.
set -uo pipefail

MODULE="${MODULE:-artaza_magento_connect}"
TEST_DB="${TEST_DB:-odoo_test}"
CONTAINER="${ODOO_CONTAINER:-odoo19}"
TAGS="${1:-/$MODULE}"
LOG="${TEST_LOG:-/tmp/odoo-tests-$MODULE.log}"

# --- Guard: never install over a database someone is using -------------------
# The suite runs with -i, which REINSTALLS the module. Against the working
# database that wipes its configuration and data. This check is the whole
# reason this script exists instead of a documented command line.
PROTECTED="${PROTECTED_DBS:-odoo postgres prueba_store ticket-ai}"
for db in $PROTECTED; do
    if [ "$TEST_DB" = "$db" ]; then
        echo "REFUSED: '$TEST_DB' is a protected database. Tests run with -i, which"
        echo "reinstalls the module and would destroy its data. Use a disposable one."
        exit 2
    fi
done

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "REFUSED: container '$CONTAINER' is not running. Start it with: docker compose up -d"
    exit 2
fi

# --- Install or update? ------------------------------------------------------
# -i only does something when the module is NOT yet installed. Run it twice
# against the same database and the second run installs nothing, executes no
# tests, and reports "0 failed, 0 error(s) of 0 tests" -- which reads green.
# So: -i to create, -u to re-run.
# psql lives in the database container, not in the Odoo one.
installed=$(docker exec "${DB_CONTAINER:-odoo-db-1}" psql -U "${DB_USER:-odoo}" -tAc \
    "SELECT state FROM ir_module_module WHERE name='$MODULE'" "$TEST_DB" 2>/dev/null | tr -d '[:space:]')
if [ "$installed" = "installed" ]; then MODE="-u"; else MODE="-i"; fi

docker exec "$CONTAINER" python3 /opt/odoo/odoo-bin -c /etc/odoo.conf \
    -d "$TEST_DB" "$MODE" "$MODULE" \
    --test-enable --test-tags "$TAGS" \
    --stop-after-init \
    --no-http --http-port="${TEST_HTTP_PORT:-8199}" \
    --max-cron-threads=0 --workers=0 \
    > "$LOG" 2>&1
status=$?

verdict=$(grep -E "[0-9]+ failed, [0-9]+ error\(s\) of [0-9]+ tests" "$LOG" | tail -1)

echo "database : $TEST_DB ($MODE)"
echo "tags     : $TAGS"
echo "log      : $LOG"
echo "verdict  : ${verdict:-NO RESULT LINE - the run died before reaching the tests}"

# Odoo's exit code and the result line are checked independently: a crash
# during module loading exits non-zero without ever printing a verdict.
if [ -z "$verdict" ]; then
    echo
    echo "--- last 20 lines ---"
    tail -20 "$LOG"
    exit 1
fi
# A run that executed nothing is not a pass. Without this check the suite can
# go green while testing literally zero lines.
if echo "$verdict" | grep -q "of 0 tests"; then
    echo
    echo "FAILED: the run executed 0 tests. Nothing was verified."
    echo "The module was neither installed nor updated - check the log."
    exit 1
fi
if ! echo "$verdict" | grep -q "0 failed, 0 error(s)"; then
    echo
    echo "--- failures ---"
    grep -E "^(FAIL|ERROR)|Traceback|AssertionError" -A5 "$LOG" | head -60
    exit 1
fi
exit $status
