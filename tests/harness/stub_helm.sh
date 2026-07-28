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
# `helm pull`/`helm template` model REAL helm's own chart-path contract
# (umbrella #296 — this stubbing used to be "intentionally minimal": `pull`
# only mkdir'd the --untardir, and `template` ignored its chart-path argument
# entirely and always echoed an empty doc). That minimality is exactly why CI
# stayed green while EVERY live topology launch failed in the capacity gate:
# capacity.yml deleted the pulled chart dir before its topology group-render
# loop ran, and the old `template` case happily "rendered" a directory that no
# longer existed. The two cases are now paired and must stay paired:
#   pull     — creates <untardir>/<chart-name>/Chart.yaml, like real
#              `helm pull --untar --untardir DIR` (chart name derived from the
#              last path segment of the chart ref).
#   template — FAILS CLOSED with real helm's own
#              `Error: path "<path>" not found` on stderr when the chart path
#              does not exist on disk; empty-manifest stdout on the happy path.
# Hardening `template` alone would break every scenario that reaches a
# capacity render (nothing would ever have created the chart subdir); relaxing
# `template` back to path-blind would silently re-blind the harness to #296.
# The rendered manifest itself is still an empty YAML doc — a scenario that
# reaches a capacity render therefore still needs `l3_override=true` for the
# resulting (genuine) missing-budget refusal; only the chart PATH contract is
# modelled here, not chart CONTENT.

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
    # `helm pull <ref> --version <v> --untar --untardir <dir> --plain-http`.
    # Real helm untars the chart into <untardir>/<chart-name>/ — that
    # subdirectory (NOT the untardir itself) is what every caller then feeds
    # to `helm template`/`helm upgrade`, so the stub must create it too, or
    # the hardened `template` case below would reject every render. The
    # chart name comes from the LAST path segment of the chart ref, which is
    # how the real registry layout works and exactly what the callers
    # themselves assume (`<untardir>/{{ l3_chart_name }}`).
    untardir=""
    chart_ref=""
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        --untardir) untardir="$2"; shift 2 ;;
        --version) shift 2 ;;
        -*) shift ;;
        *)
          if [ -z "$chart_ref" ]; then
            chart_ref="$1"
          fi
          shift
          ;;
      esac
    done
    if [ -n "$untardir" ]; then
      chart_name="${chart_ref##*/}"
      if [ -z "$chart_name" ]; then
        echo "stub_helm.sh: 'helm pull' could not derive a chart name from ref '$chart_ref'" >&2
        exit 1
      fi
      mkdir -p "$untardir/$chart_name"
      cat >"$untardir/$chart_name/Chart.yaml" <<EOF
apiVersion: v2
name: $chart_name
description: stub_helm.sh fixture chart (see this script's header)
type: application
version: 0.0.0-stub
appVersion: "0.0.0-stub"
EOF
    fi
    exit 0
    ;;

  template)
    # `helm template <release> <chart-path> -n <ns> [--set k=v ...]` — so the
    # chart path is always argv[2] (argv[0] is `template`, argv[1] the release
    # name). Fail CLOSED when it doesn't exist, byte-for-byte like real helm:
    # a stub that renders a chart directory that isn't there cannot catch an
    # ordering bug that deletes it (umbrella #296).
    chart_path="${3:-}"
    if [ ! -e "$chart_path" ]; then
      echo "Error: path \"$chart_path\" not found" >&2
      exit 1
    fi
    # Content is still not modelled — an empty document, see file header.
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
