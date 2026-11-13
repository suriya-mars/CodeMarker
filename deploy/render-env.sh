#!/usr/bin/env bash
# Write /opt/repodigger/.env from AWS Secrets Manager plus this machine's settings.
#
# Secrets live only in Secrets Manager and in this root-only file, never in git, the
# image, Terraform state, or the instance's user data. Run as root; the systemd unit
# runs it before every start, so a rotated secret takes effect on restart.
set -euo pipefail

CONF=/etc/repodigger.env          # written once by the instance's user data (no secrets)
APP_DIR=${APP_DIR:-/opt/repodigger}
# shellcheck source=/dev/null
. "$CONF"

secret=$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$SECRET_ID" --query SecretString --output text)

# Every key must be an env var name, and every value one line without a single
# quote, so it can be written as KEY='value' and read back literally.
if ! echo "$secret" | jq -e 'type == "object" and all(to_entries[];
      (.key | test("^[A-Z][A-Z0-9_]*$")) and
      ((.value | tostring) | test("[\n\r'"'"']") | not))' >/dev/null; then
  echo "render-env: the secret must be a JSON object of KEY: value, one line each, no single quotes" >&2
  exit 1
fi

missing=()
for key in NEO4J_PASSWORD; do
  echo "$secret" | jq -e --arg k "$key" '.[$k] // "" | length > 0' >/dev/null || missing+=("$key")
done
if ! echo "$secret" | jq -e '(.OPENAI_API_KEY // "") != "" or (.ANTHROPIC_API_KEY // "") != ""' >/dev/null; then
  missing+=("OPENAI_API_KEY or ANTHROPIC_API_KEY")
fi
if ((${#missing[@]})); then
  echo "render-env: missing from the secret: ${missing[*]}" >&2
  exit 1
fi

umask 077
tmp=$(mktemp "$APP_DIR/.env.XXXXXX")
{
  echo "# Written by deploy/render-env.sh from $SECRET_ID. Do not edit; edit the secret."
  # Machine settings are set below, never by the secret.
  echo "$secret" | jq -r 'del(.AUTH_MODE, .ALB_ARN, .PUBLIC_URL, .CORS_ORIGINS)
    | to_entries[] | "\(.key)='"'"'\(.value | tostring)'"'"'"'

  echo "AUTH_MODE='alb'"
  echo "ALB_ARN='$ALB_ARN'"
  echo "PUBLIC_URL='$PUBLIC_URL'"
  echo "CORS_ORIGINS=''"
} > "$tmp"
mv "$tmp" "$APP_DIR/.env"
echo "render-env: wrote $APP_DIR/.env ($(echo "$secret" | jq 'length') secret keys)"
