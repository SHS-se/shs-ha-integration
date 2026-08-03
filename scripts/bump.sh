#!/usr/bin/env bash
# Set the version in the integration manifest.
#
# Every push to main publishes the version the manifest names, and the workflow
# never edits it — so this must be bumped in the same commit as the change, or
# the Beta run fails with "Version not bumped".
#
#   scripts/bump.sh beta      0.4.0-beta.3 -> 0.4.0-beta.4   (0.4.0 -> 0.4.0-beta.1)
#   scripts/bump.sh minor     0.4.0-beta.3 -> 0.5.0
#   scripts/bump.sh patch     0.4.0 -> 0.4.1
#   scripts/bump.sh major     0.4.0 -> 1.0.0
#   scripts/bump.sh release   0.4.0-beta.4 -> 0.4.0   (drop the suffix)
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
  echo "usage: scripts/bump.sh <beta|major|minor|patch|release|X.Y.Z> [--commit]" >&2
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
  beta)
    # Continue the current series, or open one on a version without a suffix.
    if [[ "$current" =~ -beta\.([0-9]+)$ ]]; then
      next="${base}-beta.$(( BASH_REMATCH[1] + 1 ))"
    else
      next="${base}-beta.1"
    fi
    ;;
  release) next="$base" ;;
  major) next="$(( major + 1 )).0.0" ;;
  minor) next="${major}.$(( minor + 1 )).0" ;;
  patch) next="${major}.${minor}.$(( patch + 1 ))" ;;
  *)
    if [[ ! "$part" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$part is not beta, release, major, minor, patch, or an X.Y.Z version" >&2
      exit 64
    fi
    next="$part"
    ;;
esac

if [[ "$next" == "$current" ]]; then
  echo "manifest is already at $next; nothing to do" >&2
  exit 66
fi

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
  git commit -m "Set the version to $next"
fi

case "$next" in
  *-*) echo "Push to main and the Beta workflow publishes $next." ;;
  *) echo "Run the Release workflow to publish $next." ;;
esac
