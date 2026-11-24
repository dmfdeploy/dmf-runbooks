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
#   roles/nmos-cpp/scripts/push-nmos-images.sh        # tag 0.1.0
#   IMAGE_TAG=<tag> roles/nmos-cpp/scripts/push-nmos-images.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
UMBRELLA="$(cd "$REPO_ROOT/.." && pwd)"
ENV_ROOT="$UMBRELLA/dmf-env"

# The one upstream pin (see roles/nmos-cpp/NMOS_CPP_REF); no build runs without it.
NMOS_CPP_REF="$(tr -d '[:space:]' < "$REPO_ROOT/roles/nmos-cpp/NMOS_CPP_REF")"
if ! [[ "$NMOS_CPP_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: roles/nmos-cpp/NMOS_CPP_REF must hold a full 40-character commit sha" >&2
  exit 1
fi
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"

# The images record the checkout's commit, so build only from a clean, committed tree.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  echo "ERROR: $REPO_ROOT has uncommitted changes. Commit or discard them, then build again." >&2
  exit 1
fi
VCS_REF="$(git -C "$REPO_ROOT" rev-parse HEAD)"

echo "=== Step 1: Build nmos-cpp-registry ==="
export DOCKER_HOST=unix://$HOME/.colima/docker-build/docker.sock
cd "$REPO_ROOT/roles/nmos-cpp/files"
docker build --build-arg NMOS_CPP_REF="$NMOS_CPP_REF" \
  --build-arg IMAGE_VERSION="$IMAGE_TAG" --build-arg VCS_REF="$VCS_REF" \
  -t registry.dmf.example.com/dmf/nmos-cpp-registry:"$IMAGE_TAG" -f Dockerfile.registry .

echo ""
echo "=== Step 2: Build nmos-cpp-node ==="
docker build --build-arg NMOS_CPP_REF="$NMOS_CPP_REF" \
  --build-arg IMAGE_VERSION="$IMAGE_TAG" --build-arg VCS_REF="$VCS_REF" \
  -t registry.dmf.example.com/dmf/nmos-cpp-node:"$IMAGE_TAG" -f Dockerfile.node .

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
docker push registry.dmf.example.com/dmf/nmos-cpp-registry:"$IMAGE_TAG"

echo ""
echo "=== Step 7: Push nmos-cpp-node ==="
docker push registry.dmf.example.com/dmf/nmos-cpp-node:"$IMAGE_TAG"

echo ""
echo "=== Done: both images pushed to Zot ==="
