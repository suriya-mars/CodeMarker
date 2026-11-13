#!/usr/bin/env bash
# Deploy a new version: fetch it, rebuild, restart. Data volumes are kept.
#   sudo /opt/repodigger/deploy/update.sh [git ref, default: the configured branch]
set -euo pipefail
APP_DIR=${APP_DIR:-/opt/repodigger}
. /etc/repodigger.env
ref=${1:-$GIT_REF}
git -C "$APP_DIR" fetch --quiet origin
git -C "$APP_DIR" checkout --quiet --force "origin/$ref" 2>/dev/null || git -C "$APP_DIR" checkout --quiet --force "$ref"
echo "update: now at $(git -C "$APP_DIR" log -1 --format='%h %s')"
systemctl restart repodigger
