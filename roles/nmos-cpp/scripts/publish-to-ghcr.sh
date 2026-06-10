#!/usr/bin/env bash
# publish-to-ghcr.sh — push locally-built NMOS-cpp images to GHCR.
#
# Thin wrapper around the umbrella's bin/publish-image-to-ghcr.sh. Handles
# NMOS-specific concerns (registry + node image pair, pre-release tag policy
# warning); auth + push live in the umbrella script.
#
# See the umbrella script for the secrets posture (token via stdin, isolated
# DOCKER_CONFIG with cleanup trap, never argv).
#
# Tag policy:
#   The current local images may have been built before Dockerfile hardening
#   (no NMOS_CPP_REF pin). Per the public registry plan §5.1, those must NOT
#   be public-tagged as `:0.1.0`. This wrapper defaults to `:0.1.0-dev` and
#   warns if you try to push a non-prerelease tag — confirm before continuing
#   when the local images come from a master-built (unpinned) build.
#
# Usage (operator runs in their terminal):
#
#   # From a password manager:
#   <password-manager-command> \
#     | GHCR_USER="<github-username>" \
#       roles/nmos-cpp/scripts/publish-to-ghcr.sh
#
#   # Interactive:
#   roles/nmos-cpp/scripts/publish-to-ghcr.sh
#
# Env knobs:
#   GHCR_USER         GitHub username (default: prompt)
#   GHCR_NAMESPACE    GHCR namespace (default: dmfdeploy)
#   IMAGE_TAG         Tag suffix (default: 0.1.0-dev)
#   SOURCE_REGISTRY   Local registry prefix (default: registry.dmf.example.com/dmf;
#                     operators using a different local namespace override this).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ → nmos-cpp/ → roles/ → dmf-runbooks/ → dmfdeploy/
UMBRELLA_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

GHCR_NAMESPACE="${GHCR_NAMESPACE:-dmfdeploy}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0-dev}"
SOURCE_REGISTRY="${SOURCE_REGISTRY:-registry.dmf.example.com/dmf}"

# Pre-release tag policy warning ------------------------------------------
# Skipped if NMOS_FORCE_PUBLISH=1 is set (explicit operator override for
# pipelines where stdin is a token, not a TTY). Otherwise reads y/N from
# /dev/tty so the prompt works even when the token is piped in via stdin.
if [[ "${IMAGE_TAG}" != *"-dev"* && "${IMAGE_TAG}" != *"-pre"* && "${IMAGE_TAG}" != *"-rc"* ]]; then
  if [[ "${NMOS_FORCE_PUBLISH:-0}" == "1" ]]; then
    echo "ℹ️  IMAGE_TAG=\"${IMAGE_TAG}\" (non-prerelease); NMOS_FORCE_PUBLISH=1 set, proceeding." >&2
  else
    cat >&2 <<WARN
⚠️  IMAGE_TAG="${IMAGE_TAG}" does not look like a pre-release tag (e.g. -dev / -pre / -rc).

If the current local images were built without an explicit NMOS_CPP_REF pin,
the public registry plan §5.1 forbids publishing them under a canonical
public tag. Confirm the local images came from a properly pinned build.

To bypass non-interactively (e.g. token via pipe): re-run with NMOS_FORCE_PUBLISH=1.

Continue? [y/N]
WARN
    if [[ -r /dev/tty ]]; then
      read -r ANSWER < /dev/tty
    else
      echo "Aborted (no /dev/tty; set NMOS_FORCE_PUBLISH=1 to skip)." >&2
      exit 1
    fi
    if [[ "${ANSWER}" != "y" && "${ANSWER}" != "Y" ]]; then
      echo "Aborted." >&2
      exit 1
    fi
  fi
fi

# Delegate to umbrella ---------------------------------------------------

exec "${UMBRELLA_DIR}/bin/publish-image-to-ghcr.sh" \
  "${SOURCE_REGISTRY}/nmos-cpp-registry:${IMAGE_TAG}" \
  "ghcr.io/${GHCR_NAMESPACE}/nmos-cpp-registry:${IMAGE_TAG}" \
  "${SOURCE_REGISTRY}/nmos-cpp-node:${IMAGE_TAG}" \
  "ghcr.io/${GHCR_NAMESPACE}/nmos-cpp-node:${IMAGE_TAG}"
