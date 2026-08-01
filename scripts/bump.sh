#!/usr/bin/env bash
# Move the base version in the integration manifest.
#
# Betas and stable releases are cut by the Beta and Release workflows, which
# derive their versions from this number. The only thing a human decides is
# which release series comes next, and that is what this script sets.
#
#   scripts/bump.sh minor     0.4.0 -> 0.5.0
#   scripts/bump.sh patch     0.4.0 -> 0.4.1
#   scripts/bump.sh major     0.4.0 -> 1.0.0
#   scripts/bump.sh 1.2.3     set it explicitly
#
# Pass --commit to commit the change.

set -euo pipefail

manifest="$(git rev-parse --show-toplevel)/custom_components/shs_energy/manifest.json"

commit=false
part=""
for argument in "$@"; do
  case "$argument" in
    --commit) commit=true ;;
    *) part="$argument" ;;
  esac
done

if [[ -z "$part" ]]; then
  echo "usage: scripts/bump.sh <major|minor|patch|X.Y.Z> [--commit]" >&2
  exit 64
fi

current="$(python3 -c "import json; print(json.load(open('$manifest', encoding='utf-8'))['version'])")"
base="${current%%-*}"

if [[ ! "$base" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "manifest version $current has no usable X.Y.Z base" >&2
  exit 65
fi
major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
patch="${BASH_REMATCH[3]}"

case "$part" in
  major) next="$(( major + 1 )).0.0" ;;
  minor) next="${major}.$(( minor + 1 )).0" ;;
  patch) next="${major}.${minor}.$(( patch + 1 ))" ;;
  *)
    if [[ ! "$part" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$part is not major, minor, patch, or an X.Y.Z version" >&2
      exit 64
    fi
    next="$part"
    ;;
esac

VERSION="$next" MANIFEST="$manifest" python3 - <<'PY'
import json
import os
import re

path = os.environ["MANIFEST"]
version = os.environ["VERSION"]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
updated, count = re.subn(r'("version":\s*)"[^"]*"', rf'\1"{version}"', text, count=1)
if count != 1:
    raise SystemExit(f"could not find a version field in {path}")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(updated)
json.loads(updated)
PY

echo "$current -> $next"

if [[ "$commit" == true ]]; then
  git -C "$(dirname "$manifest")" add "$manifest"
  git commit -m "Start the $next release series"
fi

echo "Run the Beta workflow for $next-beta.1, or Release for $next."
