#!/usr/bin/env bash
# push-nmos-images.sh — build and push nmos-cpp images to Zot
#
# Follows the documented procedure in roles/nmos-cpp/README.md.
# Builds both registry and node images via Colima Docker, then pushes
# to the in-cluster Zot registry using isolated DOCKER_CONFIG so
# credentials don't leak into ~/.docker/config.json.
#
# Prerequisites:
#   - Colima running: colima start docker-build
#   - OpenBao breakglass file at <secure-store>/openbao-breakglass/<env>/openbao-keys-automation.json
#   - dmf-runbooks and dmf-env repos present
#
# Usage:
#   cd <dmf-runbooks-checkout>
#   roles/nmos-cpp/scripts/push-nmos-images.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
UMBRELLA="$(cd "$REPO_ROOT/.." && pwd)"
ENV_ROOT="$UMBRELLA/dmf-env"

echo "=== Step 1: Build nmos-cpp-registry ==="
export DOCKER_HOST=unix://$HOME/.colima/docker-build/docker.sock
cd "$REPO_ROOT/roles/nmos-cpp/files"
docker build -t registry.dmf.example.com/dmf/nmos-cpp-registry:0.1.0 -f Dockerfile.registry .

echo ""
echo "=== Step 2: Build nmos-cpp-node ==="
docker build -t registry.dmf.example.com/dmf/nmos-cpp-node:0.1.0 -f Dockerfile.node .

echo ""
echo "=== Step 3: Set up isolated Docker config for Zot push ==="
export DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf "$DOCKER_CONFIG"; unset DOCKER_CONFIG' EXIT

echo "DOCKER_CONFIG=$DOCKER_CONFIG (isolated, will be cleaned up on exit)"

echo ""
echo "=== Step 4: Retrieve Zot admin credentials from OpenBao ==="
cd "$ENV_ROOT"
ZOT_CREDS_JSON=$("$ENV_ROOT/bin/get-admin-cred.sh" zot)
ZOT_USER="$(echo "$ZOT_CREDS_JSON" | jq -re '.data.data.username')"
ZOT_PASS="$(echo "$ZOT_CREDS_JSON" | jq -re '.data.data.password')"

echo ""
echo "=== Step 5: Docker login to Zot ==="
echo "$ZOT_PASS" | docker login -u "$ZOT_USER" --password-stdin registry.dmf.example.com

# Clear password from env
unset ZOT_PASS
unset ZOT_CREDS_JSON

echo ""
echo "=== Step 6: Push nmos-cpp-registry ==="
docker push registry.dmf.example.com/dmf/nmos-cpp-registry:0.1.0

echo ""
echo "=== Step 7: Push nmos-cpp-node ==="
docker push registry.dmf.example.com/dmf/nmos-cpp-node:0.1.0

echo ""
echo "=== Done: both images pushed to Zot ==="
