#!/bin/sh
# darwin-agentic-cloud — container entrypoint.
#
# Materializes substrate-class signing keys from environment variables
# into the keys directory before launching the server. The keys are
# delivered to the container as Fly secrets (or Docker env vars in
# self-hosted deployments) so they never touch the image layers and
# never appear in container logs.
#
# Convention: for each allowlisted substrate, the key is read from the
# env var `DARWIN_CLASS_KEY_<NAME>` where <NAME> is the substrate_id
# uppercased with '-' replaced by '_'. Example:
#
#   substrate_id     = local-docker-v0
#   env var          = DARWIN_CLASS_KEY_LOCAL_DOCKER_V0
#   on-disk path     = ${DARWIN_CLASS_KEYS_DIR}/local-docker-v0.pem
#
# If the env var is unset, the key file is NOT written. The server
# starts normally and the /v0/sign-substrate-identity endpoint returns
# 503 for that substrate (documented behavior).

set -eu

KEYS_DIR="${DARWIN_CLASS_KEYS_DIR:-/data/class-keys}"
mkdir -p "${KEYS_DIR}"
chmod 0700 "${KEYS_DIR}"

# materialize_key <substrate_id> <env_var_name>
#   - if the env var is non-empty, write its contents to {KEYS_DIR}/<substrate_id>.pem
#   - chmod 0600 (owner read+write only)
#   - print a single line indicating what we did (no key material in logs)
materialize_key() {
    substrate_id="$1"
    env_var_name="$2"
    pem_path="${KEYS_DIR}/${substrate_id}.pem"
    # Use eval to expand the env var by name without polluting our shell.
    pem_value=$(eval "printf '%s' \"\${${env_var_name}:-}\"")
    if [ -z "${pem_value}" ]; then
        printf 'class-key bootstrap: %-32s [unset]\n' "${substrate_id}"
        return 0
    fi
    if [ -f "${pem_path}" ]; then
        # Already on disk (mounted volume from a prior boot). Skip
        # rewriting so we don't churn the file's mtime on every restart.
        # If the secret has actually rotated, the operator restarts with
        # an empty volume or deletes the file manually.
        printf 'class-key bootstrap: %-32s [already-on-disk]\n' "${substrate_id}"
        return 0
    fi
    printf '%s\n' "${pem_value}" > "${pem_path}"
    chmod 0600 "${pem_path}"
    printf 'class-key bootstrap: %-32s [materialized]\n' "${substrate_id}"
}

# Allowlisted substrates. Must stay in sync with
# darwin.agenticcloud.class_keys.ALLOWED_SUBSTRATES. When a new
# substrate is allowlisted, add a line here.
materialize_key "local-docker-v0" "DARWIN_CLASS_KEY_LOCAL_DOCKER_V0"

# Hand off to the original CMD (uvicorn / darwin serve).
exec "$@"
