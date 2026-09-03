#!/usr/bin/env bash
# Deploy staging. Triggered by GitHub Actions on a push to `staging`.
#
# This file is the versioned source. The copy that actually RUNS is installed at
# /usr/local/bin/deploy-staging, root-owned, and that separation is mechanical
# rather than political: one of the steps below is `git reset --hard`, and bash
# reads a script incrementally, so a script that rewrites its own file mid-run
# behaves unpredictably. It also means a bad merge cannot change what a deploy
# does, since the branch being deployed is not where the deploy comes from.
#
# To install after changing it:
#     scp .github/scripts/deploy-staging.sh root@HOST:/usr/local/bin/deploy-staging
#
# It prints its own checksum on every run, so drift between the installed copy
# and this one shows up in the deploy log without anything checking for it.
#
# github_user can only execute it, never write it, and its SSH key is pinned to
# this one command -- so that key can do nothing else on the machine.
#
# The backup runs here rather than in the agent: the agent triggers a deploy and
# never takes the dump itself, nor names its path.
set -euo pipefail

DIR=/var/www/staging_odoo
MODULE=artaza_magento_connect
DB=odoo
BACKUPS=/var/backups/staging
KEEP=10

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

# Printed on every run so drift between this file and the versioned source is
# visible in the deploy log, without anything having to check for it.
log "running $(sha256sum "$0" | cut -c1-12)"
fail() { log "FAILED: $*"; exit 1; }

command -v docker >/dev/null || fail "docker is not on PATH"
cd "$DIR" || fail "$DIR is unreachable"

# --- backup, before anything is touched ------------------------------------
mkdir -p "$BACKUPS"
STAMP=$(date -u +%Y%m%d-%H%M%S)
DUMP="$BACKUPS/$DB-$STAMP.dump"
log "backup"
docker exec odoo19-staging-db pg_dump -U odoo -Fc "$DB" > "$DUMP" || fail "backup failed"
chmod 600 "$DUMP"
log "backup ok ($(du -h "$DUMP" | cut -f1))"
# Keep the last N. An unbounded backup directory fills the disk, and this host
# was already at 85%.
ls -1t "$BACKUPS"/*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

# --- code -------------------------------------------------------------------
BEFORE=$(git rev-parse --short HEAD)
log "fetching origin/staging"
git fetch --depth 1 origin staging || fail "fetch failed"
git reset --hard origin/staging || fail "reset failed"
AFTER=$(git rev-parse --short HEAD)
log "$BEFORE -> $AFTER"

if [ "$BEFORE" = "$AFTER" ]; then
    log "nothing new, redeploying anyway"
fi

# --- containers -------------------------------------------------------------
log "compose up"
docker compose up -d --build || fail "compose up failed"

log "waiting for postgres"
for _ in $(seq 1 120); do
    docker exec odoo19-staging-db pg_isready -U odoo -q 2>/dev/null && break
done

# --- module -----------------------------------------------------------------
# -u is not optional: a change under static/ is invisible until the module is
# updated, and restarting the server is not enough. The odd http-port keeps this
# one-off run from fighting the live server for a socket.
log "updating $MODULE"
docker exec odoo19-staging python3 /opt/odoo/odoo-bin -c /etc/odoo.conf -d "$DB" \
    -u "$MODULE" --stop-after-init \
    --no-http --http-port=8199 --max-cron-threads=0 --workers=0 \
    || fail "module update failed"

log "restarting"
docker compose restart odoo || fail "restart failed"

# --- health -----------------------------------------------------------------
log "health check"
for _ in $(seq 1 120); do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8070/web/login || true)
    if [ "$CODE" = "200" ]; then
        log "OK — staging serves 200 at $AFTER"
        exit 0
    fi
done
fail "staging never returned 200; the dump from this run is at $DUMP"
