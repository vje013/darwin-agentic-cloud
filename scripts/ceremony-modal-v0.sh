#!/usr/bin/env bash
# Fly class-key ceremony for the modal-v0 substrate.
#
# Run from anywhere on the operator laptop; the script cds to the repo root.
# Needs: uv (project env), flyctl logged in to the darwin-agentic-cloud org,
# curl, python3.
#
# What it does, in order:
#   1. generate   Ed25519 class key into a private temp dir (never the repo)
#   2. stage      upload the PEM as the Fly secret DARWIN_CLASS_KEY_MODAL_V0
#                 without restarting the app
#   3. deploy     build + deploy, so the new entrypoint materializes the key
#                 into /data/class-keys/modal-v0.pem on the persistent volume
#   4. verify     fetch the public keylist and confirm it carries this key
#   5. cleanup    delete the local private key (trap, runs on any exit)
#
# Private key material is never printed. After the run it exists only on Fly.
# Refuses to run if the live keylist or Fly already has a modal-v0 key; use
# `darwin admin class-keys rotate` for a replacement, not this script.

set -euo pipefail

APP="${DAC_FLY_APP:-darwin-agentic-cloud}"
SUBSTRATE="modal-v0"
ENV_VAR="DARWIN_CLASS_KEY_MODAL_V0"
KEYLIST_URL="${DAC_KEYLIST_URL:-https://${APP}.fly.dev/.well-known/substrate-keys.json}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

FLY="$(command -v fly || command -v flyctl || true)"
[ -n "$FLY" ] || { echo "error: flyctl not found on PATH" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: uv not found on PATH" >&2; exit 1; }
command -v curl >/dev/null || { echo "error: curl not found on PATH" >&2; exit 1; }

# --- guard: the entrypoint in this checkout must materialize modal-v0 ---
grep -q "^materialize_key \"${SUBSTRATE}\" \"${ENV_VAR}\"" docker-entrypoint.sh || {
    echo "error: docker-entrypoint.sh has no materialize_key line for ${SUBSTRATE}; deploy would not pick the key up" >&2
    exit 1
}

# --- guard: never generate a second active key by accident ---
echo "[0/5] checking live keylist at ${KEYLIST_URL}"
if curl -fsS --max-time 60 "$KEYLIST_URL" | python3 -c '
import json, sys
keys = json.load(sys.stdin).get("keys", [])
sys.exit(0 if any(k.get("substrate_id") == "modal-v0" and k.get("status") == "active" for k in keys) else 1)
'; then
    echo "error: live keylist already has an active ${SUBSTRATE} key." >&2
    echo "       Use: uv run darwin admin class-keys rotate ${SUBSTRATE}   (then upload + deploy)" >&2
    exit 1
fi
echo "[0/5] checking Fly access to ${APP}"
if ! secrets_out="$("$FLY" secrets list -a "$APP" 2>&1)"; then
    echo "error: cannot read secrets for ${APP}: ${secrets_out}" >&2
    echo "       logged in as : $("$FLY" auth whoami 2>&1)" >&2
    echo "       apps visible : $("$FLY" apps list 2>&1 | tail -n +2 | awk '{print $1}' | tr '\n' ' ')" >&2
    echo "       If ${APP} is not in that list, this login does not own it:" >&2
    echo "         ${FLY} auth logout && ${FLY} auth login    (pick the account that created the app)" >&2
    echo "       If it is listed, the stored token is stale after the flyctl upgrade:" >&2
    echo "         ${FLY} auth login" >&2
    echo "       Nothing was generated or uploaded." >&2
    exit 1
fi
if printf '%s\n' "$secrets_out" | awk '{print $1}' | grep -qx "$ENV_VAR"; then
    echo "error: Fly secret ${ENV_VAR} already exists on ${APP} but the keylist has no active key." >&2
    echo "       Either the deploy that materializes it has not happened (run: ${FLY} deploy -a ${APP})" >&2
    echo "       or the secret is stale (run: ${FLY} secrets unset ${ENV_VAR} -a ${APP}) and rerun." >&2
    exit 1
fi

KEYDIR="$(mktemp -d)"
chmod 0700 "$KEYDIR"
cleanup() { rm -rf "$KEYDIR"; echo "[5/5] local private key deleted"; }
trap cleanup EXIT

echo "[1/5] generating ${SUBSTRATE} class key"
uv run darwin admin class-keys generate "$SUBSTRATE" --out-dir "$KEYDIR"

echo "[2/5] staging Fly secret ${ENV_VAR} on ${APP} (no restart)"
"$FLY" secrets set --stage -a "$APP" "${ENV_VAR}=$(cat "${KEYDIR}/${SUBSTRATE}.pem")"

echo "[3/5] deploying ${APP}"
"$FLY" deploy -a "$APP"

echo "[4/5] verifying against live keylist"
attempt=0
until uv run darwin admin class-keys verify "$SUBSTRATE" "$KEYLIST_URL" --keys-dir "$KEYDIR"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 6 ]; then
        echo "error: keylist did not show the new key after 6 attempts; check: ${FLY} logs -a ${APP}" >&2
        exit 1
    fi
    echo "  not visible yet (machine may be waking); retry ${attempt}/6 in 10s"
    sleep 10
done

echo
echo "done: ${SUBSTRATE} class key is live at ${KEYLIST_URL}"
echo "      private material now exists only on Fly (secret ${ENV_VAR}, volume /data/class-keys)"
