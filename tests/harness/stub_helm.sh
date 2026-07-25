#!/usr/bin/env bash
# Stub `helm` binary for the l3_run_guard test harness.
#
# Prepend tests/harness/ to PATH ahead of the real helm during a test
# ansible-playbook run so every `ansible.builtin.command: argv: [helm, ...]`
# call in roles/l3_run_guard's tasks (snapshot.yml, rollback_helm_surface.yml,
# capacity.yml) — and every `kubernetes.core.helm` module invocation, which
# itself just shells out to `helm ...` — hits this script instead.
#
# Exact argv shapes handled (grepped from the real task files, see
# tests/harness/README.md for citations):
#   helm list -n <ns> -o json
#   helm list -n <ns> -q
#   helm get values <release> -n <ns> -o json
#   helm rollback <release> <revision> -n <ns> --wait --timeout 2m
#   helm uninstall <release> [-n <ns>] [--wait] [--timeout ...] ...   (kubernetes.core.helm module)
#   helm pull <ref> --version <v> --untar --untardir <dir> --plain-http
#   helm template <release> <path> -n <ns> [--set k=v ...]
#   helm upgrade --install <release> <path> -n <ns> [--set k=v ...] ...
#     (both the raw `ansible.builtin.command` form the mxl launch playbook
#     uses, and whatever argv `kubernetes.core.helm`'s module invocation
#     itself shells out to for nmos-cpp/nmos-crosspoint — tolerant of any
#     flag set/order, always succeeds)
#
# Fixture sourcing (all via environment variables, set by the calling
# playbook's `environment:` block or shell export before ansible-playbook):
#   L3_STUB_HELM_LIST_<NS>              JSON array, e.g. L3_STUB_HELM_LIST_MXL
#   L3_STUB_HELM_VALUES_<RELEASE>       JSON object, release name upper +
#                                       dashes->underscores; "__FAIL__" to
#                                       simulate a fetch failure
#   L3_STUB_HELM_ROLLBACK_FAIL=1        simulate `helm rollback` failure
#   L3_STUB_HELM_UNINSTALL_FAIL=1       simulate `helm uninstall` failure
#   L3_STUB_HELM_UPGRADE_FAIL=1         simulate `helm upgrade` failure (umbrella #201 WP4b)
#   L3_STUB_HELM_LOG=<path>             when set, EVERY invocation (any
#                                       subcommand) appends its full
#                                       received argv as one line to this
#                                       file, unconditionally, before any
#                                       subcommand-specific handling runs —
#                                       lets a test assert on the EXACT
#                                       argv (e.g. --set values) a
#                                       `template` render and a later
#                                       `upgrade`/`install` call each
#                                       actually received, in call order.
#
# TODO: `helm pull`/`helm template` stubbing is intentionally minimal (empty
# untar dir / empty YAML doc) — capacity.yml's chart-fetch-and-render path is
# lower priority per the harness spec; flesh out with real fixture charts if
# capacity.yml control flow needs exercising later.

set -euo pipefail

subcommand="${1:-}"

# Unconditional invocation logging (see L3_STUB_HELM_LOG above) — first
# thing this script does, before the subcommand dispatch below, so every
# call (template, pull, upgrade, whatever) is captured in order regardless
# of which case handles it or whether that case itself succeeds or fails.
if [ -n "${L3_STUB_HELM_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$L3_STUB_HELM_LOG"
fi

# Uppercase + replace non-alnum with underscore, for env-var name derivation.
_envname() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'
}

case "$subcommand" in
  list)
    ns=""
    want_quiet=false
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -n|--namespace)
          ns="$2"
          shift 2
          ;;
        -o)
          # Only `-o json` is used by this role; the value itself doesn't
          # change behavior here (JSON is always what's stored/emitted).
          shift 2
          ;;
        -q)
          want_quiet=true
          shift
          ;;
        *)
          shift
          ;;
      esac
    done
    envvar="L3_STUB_HELM_LIST_$(_envname "$ns")"
    list_json="${!envvar:-[]}"
    if [ "$want_quiet" = true ]; then
      printf '%s' "$list_json" | python3 -c '
import json, sys
items = json.load(sys.stdin)
for item in items:
    print(item.get("name", ""))
'
    else
      printf '%s\n' "$list_json"
    fi
    exit 0
    ;;

  get)
    resource="${2:-}"
    if [ "$resource" != "values" ]; then
      echo "stub_helm.sh: unsupported 'helm get $resource'" >&2
      exit 1
    fi
    release="${3:-}"
    shift 3
    ns=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -n|--namespace) ns="$2"; shift 2 ;;
        -o) shift 2 ;;
        *) shift ;;
      esac
    done
    envvar="L3_STUB_HELM_VALUES_$(_envname "$release")"
    values_json="${!envvar:-{\}}"
    if [ "$values_json" = "__FAIL__" ]; then
      echo "stub_helm.sh: simulated 'helm get values' failure for release '$release'" >&2
      exit 1
    fi
    printf '%s\n' "$values_json"
    exit 0
    ;;

  rollback)
    release="${2:-}"
    revision="${3:-}"
    if [ "${L3_STUB_HELM_ROLLBACK_FAIL:-0}" = "1" ]; then
      echo "stub_helm.sh: simulated 'helm rollback' failure for release '$release'" >&2
      exit 1
    fi
    echo "Rollback was a success! Happy Helming! (stub: $release -> revision $revision)"
    exit 0
    ;;

  uninstall)
    release="${2:-}"
    if [ "${L3_STUB_HELM_UNINSTALL_FAIL:-0}" = "1" ]; then
      echo "stub_helm.sh: simulated 'helm uninstall' failure for release '$release'" >&2
      exit 1
    fi
    echo "release \"$release\" uninstalled (stub)"
    exit 0
    ;;

  upgrade)
    # `helm upgrade --install <release> <chart> -n <ns> [--set k=v ...]`
    # (raw command form) or whatever kubernetes.core.helm's module itself
    # shells out to. Same tolerant style as `uninstall` above — not strict
    # about exact flags/order, always succeeds unless
    # L3_STUB_HELM_UPGRADE_FAIL=1 (umbrella #201 WP4b — the switch
    # playbook's own re-point step needs to prove a FAILED
    # `helm upgrade --atomic` propagates correctly; this stub cannot
    # itself model Helm's own --atomic rollback-to-pre-values behavior,
    # only that the surrounding playbook correctly requests atomicity
    # (via L3_STUB_HELM_LOG) and correctly treats a failure as a
    # failure). $2 is often `--install` rather than the release name, so
    # this doesn't try to parse it out; callers that need the concrete
    # argv (e.g. to compare --set/--atomic values against a `template`
    # call) read it back via L3_STUB_HELM_LOG instead.
    if [ "${L3_STUB_HELM_UPGRADE_FAIL:-0}" = "1" ]; then
      echo "stub_helm.sh: simulated 'helm upgrade' failure" >&2
      exit 1
    fi
    echo "Release \"${2:-unknown}\" has been upgraded. Happy Helming! (stub)"
    exit 0
    ;;

  pull)
    # TODO (low priority, see file header): minimal stub — just create the
    # requested --untardir so the caller's own cleanup `file: state: absent`
    # has something real to remove, without producing an actual chart.
    untardir=""
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --untardir) untardir="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ -n "$untardir" ]; then
      mkdir -p "$untardir"
    fi
    exit 0
    ;;

  template)
    # TODO (low priority, see file header): echoes a minimal empty YAML
    # document rather than actually rendering a chart.
    echo "# stub_helm.sh: empty rendered manifest"
    exit 0
    ;;

  version)
    echo 'version.BuildInfo{Version:"v3.99.0-stub"}'
    exit 0
    ;;

  *)
    echo "stub_helm.sh: unsupported subcommand '$subcommand' (argv: $*)" >&2
    exit 1
    ;;
esac
