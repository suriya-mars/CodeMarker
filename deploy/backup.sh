#!/usr/bin/env bash
# Full, consistent Neo4j backup to S3.
#
# Neo4j Community can only dump a STOPPED database, so this stops the api and the
# database for about a minute (run it at a quiet hour; the timer says when). Daily
# EBS snapshots of the data volume run separately, with no downtime.
#
# Restore: see deploy/aws/README.md.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/repodigger}
CONF=${CONF:-/etc/repodigger.env}
[ -f "$CONF" ] && . "$CONF"        # BACKUP_BUCKET, AWS_REGION
COMPOSE=(docker compose -f "$APP_DIR/docker-compose.prod.yml" --project-directory "$APP_DIR")
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK=$(mktemp -d /var/tmp/repodigger-backup.XXXXXX)
# The neo4j image writes as its own user (uid 7474), which cannot write a root-only
# directory. Hand it over; the dump is deleted when this script exits.
chown 7474:7474 "$WORK" 2>/dev/null || chmod 0777 "$WORK"
# Whatever happens (failure, Ctrl-C, systemd stopping the job), bring the app back.
# The unit file also does this, in case this shell is killed outright.
cleanup() { rm -rf "$WORK"; "${COMPOSE[@]}" up -d neo4j api >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

"${COMPOSE[@]}" stop api neo4j
"${COMPOSE[@]}" run --rm --no-deps -v "$WORK:/backups" neo4j \
  neo4j-admin database dump neo4j --to-path=/backups
"${COMPOSE[@]}" up -d neo4j api

file="$WORK/neo4j.dump"
[ -s "$file" ] || { echo "backup: the dump is empty" >&2; exit 1; }
if [ -n "${BACKUP_BUCKET:-}" ]; then
  aws s3 cp --only-show-errors "$file" "s3://$BACKUP_BUCKET/neo4j/neo4j-$STAMP.dump"
  echo "backup: uploaded s3://$BACKUP_BUCKET/neo4j/neo4j-$STAMP.dump ($(du -h "$file" | cut -f1))"
else
  cp "$file" "${BACKUP_DIR:-/var/tmp}/neo4j-$STAMP.dump"
  echo "backup: no BACKUP_BUCKET; kept ${BACKUP_DIR:-/var/tmp}/neo4j-$STAMP.dump"
fi
