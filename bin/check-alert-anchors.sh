#!/usr/bin/env bash
# check-alert-anchors.sh — fail when an alert links to a section that does not exist.
#
# Every alert in the dmf-infra Prometheus role sets runbook_url to
# docs/alerts.md in this repository plus an anchor. The script fetches the
# rule template from the main branch of dmf-infra, collects the anchors and
# fails for each one that has no heading in docs/alerts.md.
#
# Usage:
#   bin/check-alert-anchors.sh              fetch the template, check docs/alerts.md
#   bin/check-alert-anchors.sh --self-test  check the heading-to-anchor rules
#
# Environment:
#   ALERTS_TEMPLATE_URL  where to fetch the rule template (HTTPS)
#   ALERTS_PAGE          the page to check (default docs/alerts.md)
#
# Anchors follow GitHub's rules for headings: lowercase, spaces to hyphens,
# punctuation dropped. Hyphens and underscores stay. The rules here cover
# ASCII headings, which is all the page uses. A repeated heading gets a
# numeric suffix on GitHub, and the script does not model that: it reports
# a duplicate anchor as an error.
set -euo pipefail
export LC_ALL=C

template_url="${ALERTS_TEMPLATE_URL:-https://raw.githubusercontent.com/dmfdeploy/dmf-infra/main/k3s-lab-bootstrap/roles/base/prometheus/templates/rules/alerts.yaml.j2}"
page="${ALERTS_PAGE:-docs/alerts.md}"

# Print the GitHub anchor of every heading in a Markdown file, one per line.
# Headings inside fenced code blocks do not count.
heading_anchors() {
  awk '
    /^```/ || /^~~~/ { fenced = !fenced; next }
    fenced { next }
    /^#{1,6}[ \t]/ {
      sub(/^#+[ \t]+/, "")
      sub(/[ \t]+#*[ \t]*$/, "")
      print
    }
  ' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9 _-]//g' -e 's/ /-/g'
}

self_test() {
  local dir actual expected
  dir="$(mktemp -d)"
  trap 'rm -rf "$dir"' RETURN
  cat >"$dir/page.md" <<'EOF'
# Title Case Heading
## `HostMemoryPressure`
### openbao-token-revoker
## What it is: a test (2024)?
```bash
# not a heading
```
EOF
  expected=$'title-case-heading\nhostmemorypressure\nopenbao-token-revoker\nwhat-it-is-a-test-2024'
  actual="$(heading_anchors "$dir/page.md")"
  if [ "$actual" != "$expected" ]; then
    printf 'self-test failed.\nexpected:\n%s\nactual:\n%s\n' "$expected" "$actual" >&2
    return 1
  fi
  echo "self-test passed"
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
  exit
fi

if [ ! -f "$page" ]; then
  echo "error: $page does not exist" >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl --fail --silent --show-error --location --proto '=https' --max-time 30 \
  --output "$work/alerts.yaml.j2" "$template_url"

# Each alert names its section as {{ prometheus_alert_runbook_base }}#anchor.
grep -o 'prometheus_alert_runbook_base }}#[A-Za-z0-9_-]*' "$work/alerts.yaml.j2" \
  | sed 's/.*#//' | sort -u >"$work/wanted"

if [ ! -s "$work/wanted" ]; then
  echo "error: no runbook anchors found in the template; its format may have changed" >&2
  exit 1
fi

heading_anchors "$page" | sort >"$work/have"

duplicates="$(uniq -d "$work/have")"
missing="$(comm -23 "$work/wanted" <(uniq "$work/have"))"

status=0
if [ -n "$duplicates" ]; then
  echo "error: $page has headings with the same anchor:" >&2
  sed 's/^/  /' <<<"$duplicates" >&2
  status=1
fi
if [ -n "$missing" ]; then
  echo "error: $page has no heading for these alert anchors:" >&2
  sed 's/^/  /' <<<"$missing" >&2
  status=1
fi
if [ "$status" -eq 0 ]; then
  echo "ok: $(wc -l <"$work/wanted" | tr -d ' ') alert anchors all have a heading in $page"
fi
exit "$status"
