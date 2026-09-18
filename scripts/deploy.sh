#!/usr/bin/env bash
# Deploy the committed integration to Home Assistant: push main, wait for the
# Beta workflow to publish the manifest version, download that version through
# HACS, and restart Home Assistant.
#
#   scripts/deploy.sh
#
# Commit the change with its version bump first (scripts/bump.sh beta --commit);
# uncommitted work is never deployed. Re-running is safe: finished steps are
# skipped, so a run that stopped part-way picks up where it left off.
#
# Home Assistant is reached over SSH through the Advanced SSH & Web Terminal
# add-on, whose Supervisor token authorizes everything else, so no Home
# Assistant credentials are needed: only an SSH key in the add-on's
# authorized_keys. HA_SSH overrides the destination, hassio@homeassistant.

set -euo pipefail

manifest="custom_components/shs_energy/manifest.json"
domain="shs_energy"
update_entity="update.smart_home_solutions_energy_update"
workflow="beta.yml"
ha_ssh="${HA_SSH:-hassio@homeassistant}"
ssh_options=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
published() { gh release view "$1" >/dev/null 2>&1; }

# The next two functions run on Home Assistant, as root inside the add-on.

# Calls the Supervisor API; paths under core/api/ reach Home Assistant's REST API.
supervisor_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS --fail-with-body --max-time 1800 -X "$method"
    -H "Authorization: Bearer $SUPERVISOR_TOKEN")
  if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" --data "$body")
  fi
  curl "${args[@]}" "http://supervisor/$path"
}

install_on_home_assistant() {
  local version="$1" update_entity="$2" domain="$3"
  local config entity installed request check response started entries attempt
  local restart=true

  # The add-on hands the token only to root sessions; other users reach root
  # through sudo without it, so load it from the add-on's profile.
  [[ -n "${SUPERVISOR_TOKEN:-}" ]] || source /etc/profile.d/homeassistant.sh

  config="$(supervisor_api GET core/api/config)" || fail "Home Assistant's API did not answer"
  jq -r '"Home Assistant \(.version) is \(.state | ascii_downcase)."' <<<"$config"
  [[ "$(jq -r .state <<<"$config")" == RUNNING ]] \
    || fail "wait for Home Assistant to finish starting, then run this again"

  step "Downloading $version through HACS"
  entity="$(supervisor_api GET "core/api/states/$update_entity")" \
    || fail "could not read $update_entity; is the integration installed through HACS?"
  installed="$(jq -r '.attributes.installed_version // empty' <<<"$entity")"
  if [[ "$installed" == "$version" ]]; then
    echo "$version is already downloaded."
    # HACS flags every download as needing a restart until Home Assistant restarts.
    [[ "$(jq -r '.attributes.release_summary // empty' <<<"$entity")" == *Restart* ]] || restart=false
  else
    echo "Replacing ${installed:-nothing} with $version."
    # Naming the version works whether or not HACS shows betas, and before HACS
    # has noticed the release.
    request="$(jq -cn --arg entity_id "$update_entity" --arg version "$version" \
      '{entity_id: $entity_id, version: $version}')"
    if ! supervisor_api POST core/api/services/update/install "$request" >/dev/null; then
      # A failed service call is a bare 500; the reason is only in the log.
      supervisor_api GET core/logs/latest | tail -n 25 >&2 || true
      fail "HACS could not download $version; the end of the log is above"
    fi
    installed="$(supervisor_api GET "core/api/states/$update_entity" | jq -r '.attributes.installed_version // empty')"
    [[ "$installed" == "$version" ]] || fail "HACS reports ${installed:-nothing} after downloading $version"
  fi

  step "Restarting Home Assistant"
  if [[ "$restart" == false ]]; then
    echo "$version is already running."
  else
    check="$(supervisor_api POST core/api/config/core/check_config '{}')" \
      || fail "could not check the configuration"
    if [[ "$(jq -r .result <<<"$check")" != valid ]]; then
      jq -r .errors <<<"$check" >&2
      fail "the configuration is invalid, so Home Assistant was not restarted; $version loads on the next restart"
    fi
    started=$SECONDS
    # The Supervisor answers once Home Assistant is running again.
    if ! response="$(supervisor_api POST core/restart '{}')"; then
      echo "$response" >&2
      fail "Home Assistant did not start again; check its log"
    fi
    echo "Home Assistant was running again after $(( SECONDS - started ))s."
  fi

  step "Checking $domain"
  # Home Assistant reports RUNNING while slow integrations are still setting up.
  for (( attempt = 0; attempt < 30; attempt++ )); do
    entries="$(supervisor_api GET "core/api/config/config_entries/entry?domain=$domain" \
      | jq 'map(select(.disabled_by == null))')"
    jq -e 'all(.[]; .state != "not_loaded" and .state != "setup_in_progress")' <<<"$entries" >/dev/null && break
    sleep 3
  done
  jq -r '.[] | "\(.title): \(.state)" + (if .reason then " (\(.reason))" else "" end)' <<<"$entries"
  # What the integration has logged since Home Assistant started, minus the
  # warning Home Assistant prints for every custom integration.
  supervisor_api GET core/logs/latest \
    | grep -E "(WARNING|ERROR|CRITICAL).*$domain" \
    | grep -v "has not been tested by Home Assistant" \
    | tail -n 20 || true
  jq -e 'all(.[]; .state == "loaded")' <<<"$entries" >/dev/null || fail "$domain did not load"
}

cd "$(git rev-parse --show-toplevel)"
for tool in gh jq; do
  command -v "$tool" >/dev/null || fail "$tool is required: brew install $tool"
done

step "Pushing main"
[[ "$(git symbolic-ref --quiet --short HEAD || true)" == main ]] \
  || fail "check out main: the Beta workflow only publishes from main"
[[ -z "$(git status --porcelain -- custom_components)" ]] \
  || fail "custom_components has uncommitted changes; commit them with the version bump first"
git fetch --quiet origin main
git merge-base --is-ancestor origin/main HEAD \
  || fail "origin/main has commits that main lacks; run git pull --rebase first"

version="$(git show "HEAD:$manifest" | jq -r .version)"
sha="$(git rev-parse HEAD)"
if [[ "$(git rev-parse origin/main)" == "$sha" ]]; then
  echo "origin/main is already up to date."
else
  # The Beta workflow builds every push that changes more than Markdown, and
  # fails unless the manifest names a pre-release that is not yet published.
  changed="$(git diff --name-only origin/main HEAD)"
  if [[ -n "$changed" ]] && grep -qv '\.md$' <<<"$changed"; then
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+$ ]] \
      || fail "$version is not a pre-release, so the Beta workflow would reject it; run scripts/bump.sh beta --commit"
    if published "$version"; then
      fail "$version is already published, so the Beta workflow would reject this push; run scripts/bump.sh beta --commit"
    fi
  fi
  git log --oneline origin/main..HEAD
  git push origin main
fi

step "Waiting for the Beta workflow to publish $version"
if published "$version"; then
  echo "$version is already published."
else
  run=""
  # A push takes a few seconds to start its run.
  for (( attempt = 0; attempt < 20; attempt++ )); do
    run="$(gh run list --workflow "$workflow" --commit "$sha" --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
    [[ -z "$run" ]] || break
    sleep 3
  done
  [[ -n "$run" ]] \
    || fail "no Beta workflow run started for $sha; Markdown-only pushes are not built (start one with: gh workflow run $workflow)"
  if ! gh run watch "$run" --compact --exit-status --interval 5; then
    # The failing step's own output, up to its first error.
    gh run view "$run" --log-failed | cut -f3- | cut -d' ' -f2- \
      | awk '/##\[endgroup\]/ { n = 0; next } { line[++n] = $0 }
        /##\[error\]/ { for (i = (n > 30 ? n - 29 : 1); i <= n; i++) print line[i]; exit }' >&2 || true
    fail "the Beta workflow failed: $(gh run view "$run" --json url --jq .url)"
  fi
  published "$version" || fail "the Beta workflow finished without publishing $version"
fi

step "Connecting to Home Assistant at $ha_ssh"
remote="set -euo pipefail
$(declare -f step fail supervisor_api install_on_home_assistant)
install_on_home_assistant \"\$@\""
status=0
ssh "${ssh_options[@]}" "$ha_ssh" sudo -n bash -s -- "$version" "$update_entity" "$domain" \
  <<<"$remote" || status=$?
if (( status == 255 )); then
  fail "could not SSH to $ha_ssh. If the key was refused, add your public key (e.g. ~/.ssh/id_ed25519.pub) to authorized_keys in the Advanced SSH & Web Terminal add-on's configuration, then restart the add-on"
fi
(( status == 0 )) || exit "$status"

step "Deployed $version"
