#!/usr/bin/env bash
# Deploy the committed integration and SHS app to Home Assistant: push main,
# wait for the workflows to publish the versions it names, install them, and
# restart what changed.
#
#   scripts/deploy.sh
#
# Two things are versioned and published separately, and either may be new:
#
#   integration  custom_components/shs_energy/manifest.json  Beta workflow, a
#                GitHub release that HACS downloads; Home Assistant restarts
#   app          web/package.json and apps/shs_energy/config.yaml  SHS app
#                workflow, a container image that the Supervisor updates to
#
# Commit the change with its version bump first (scripts/bump.sh beta --commit
# for the integration; the app version by hand in web/package.json,
# web/package-lock.json, apps/shs_energy/config.yaml and
# apps/shs_energy/Dockerfile); uncommitted work is never deployed. Commits that
# touch only the integration's tests are held back while its version is already
# published, and go out with the next bump. Re-running is safe: finished steps
# are skipped, so a run that stopped part-way picks up where it left off.
#
# Home Assistant is reached as root over the Home Assistant OS host's SSH on
# port 22222, which the HassOS SSH port 22222 Configurator add-on sets up. The
# Home Assistant steps run inside the Advanced SSH & Web Terminal add-on's
# container, whose Supervisor token authorizes them, so no Home Assistant
# credentials are needed. HA_HOST and HA_PORT override the destination,
# 192.168.10.20 on port 22222.

set -euo pipefail

manifest="custom_components/shs_energy/manifest.json"
app_manifest="web/package.json"
domain="shs_energy"
update_entity="update.smart_home_solutions_energy_update"
app_slug="shs_energy"
app_image="shs-se/shs-energy-app"
# What starts each workflow, as its own `paths` filter lists them.
beta_paths='^(custom_components/|tests/|scripts/bump\.sh$|\.github/workflows/beta\.yml$)'
app_paths='^(app/|web/|apps/|repository\.yaml$|\.dockerignore$|\.github/workflows/app\.yml$|scripts/build-companion\.py$|scripts/check-app\.py$)'
ha_host="${HA_HOST:-192.168.10.20}"
ha_port="${HA_PORT:-22222}"
# The Advanced SSH & Web Terminal add-on, whose token may call Home Assistant.
api_container="app_a0d7b954_ssh"
ssh_options=(-p "$ha_port" -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
published() { gh release view "$1" >/dev/null 2>&1; }

# The app image is public, so the registry answers an anonymous pull token.
app_published() {
  local token
  token="$(curl -fsS --max-time 20 "https://ghcr.io/token?scope=repository:$app_image:pull" | jq -r .token)" \
    || fail "could not reach ghcr.io to look for $app_image:$1"
  curl -fsS --max-time 20 -o /dev/null -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "https://ghcr.io/v2/$app_image/manifests/$1" 2>/dev/null
}

# Follows the run a workflow started for this push, and shows why it failed.
wait_for_workflow() {
  local workflow="$1" name="$2" run="" attempt
  # A push takes a few seconds to start its run.
  for (( attempt = 0; attempt < 20; attempt++ )); do
    run="$(gh run list --workflow "$workflow" --commit "$sha" --limit 1 --json databaseId --jq '.[0].databaseId // empty')"
    [[ -z "$run" ]] || break
    sleep 3
  done
  [[ -n "$run" ]] \
    || fail "no $name workflow run started for $sha; pushes that change none of its files are not built (start one with: gh workflow run $workflow)"
  if ! gh run watch "$run" --compact --exit-status --interval 5; then
    # The failing step's own output, up to its first error.
    gh run view "$run" --log-failed | cut -f3- | cut -d' ' -f2- \
      | awk '/##\[endgroup\]/ { n = 0; next } { line[++n] = $0 }
        /##\[error\]/ { for (i = (n > 30 ? n - 29 : 1); i <= n; i++) print line[i]; exit }' >&2 || true
    fail "the $name workflow failed: $(gh run view "$run" --json url --jq .url)"
  fi
}

# The next three functions run on Home Assistant, as root inside the add-on's
# container, where the Supervisor provides SUPERVISOR_TOKEN.

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

update_app_on_home_assistant() {
  local version="$1" suffix="$2"
  local slug info installed latest attempt

  step "Updating the SHS app to $version"
  # A repository's apps are prefixed with a hash of its URL.
  slug="$(supervisor_api GET addons \
    | jq -r --arg suffix "_$suffix" 'first(.data.addons[] | select(.slug | endswith($suffix)) | .slug) // empty')" \
    || fail "the Supervisor did not list the installed apps"
  if [[ -z "$slug" ]]; then
    echo "The SHS app is not installed on this Home Assistant; nothing to update."
    return
  fi
  info="$(supervisor_api GET "addons/$slug/info")" || fail "could not read $slug"
  installed="$(jq -r .data.version <<<"$info")"
  if [[ "$installed" == "$version" ]]; then
    echo "$version is already installed ($(jq -r .data.state <<<"$info"))."
  else
    echo "Replacing $installed with $version."
    # The store learns of a version by pulling the repository it was added from.
    supervisor_api POST store/reload '{}' >/dev/null || fail "the Supervisor could not reload its app store"
    for (( attempt = 0; attempt < 20; attempt++ )); do
      latest="$(supervisor_api GET "addons/$slug/info" | jq -r .data.version_latest)"
      [[ "$latest" == "$version" ]] && break
      sleep 3
    done
    [[ "$latest" == "$version" ]] \
      || fail "the app store offers $latest, not $version; it reads apps/shs_energy/config.yaml from the repository's default branch"
    if ! supervisor_api POST "store/addons/$slug/update" '{}' >/dev/null; then
      supervisor_api GET "addons/$slug/logs" | tail -n 25 >&2 || true
      fail "the Supervisor could not update $slug to $version"
    fi
    info="$(supervisor_api GET "addons/$slug/info")" || fail "could not read $slug"
    installed="$(jq -r .data.version <<<"$info")"
    [[ "$installed" == "$version" ]] || fail "the Supervisor reports $installed after updating to $version"
  fi
  # An app that was running is started again by the update; one that was
  # stopped stays stopped, and that is the household's choice to keep.
  if [[ "$(jq -r .data.state <<<"$info")" != started ]]; then
    echo "The SHS app is $(jq -r .data.state <<<"$info"); start it in Home Assistant to run $version."
    return
  fi
  # Its own log since it started, where a failed start explains itself.
  supervisor_api GET "addons/$slug/logs" | grep -E "(WARNING|ERROR|CRITICAL|Traceback)" | tail -n 20 || true
  echo "The SHS app is running $version."
}

cd "$(git rev-parse --show-toplevel)"
for tool in gh jq curl; do
  command -v "$tool" >/dev/null || fail "$tool is required: brew install $tool"
done

step "Pushing main"
[[ "$(git symbolic-ref --quiet --short HEAD || true)" == main ]] \
  || fail "check out main: the workflows only publish from main"
[[ -z "$(git status --porcelain -- custom_components app web apps)" ]] \
  || fail "the integration or the app has uncommitted changes; commit them with the version bump first"
git fetch --quiet origin main
git merge-base --is-ancestor origin/main HEAD \
  || fail "origin/main has commits that main lacks; run git pull --rebase first"

version="$(git show "HEAD:$manifest" | jq -r .version)"
app_version="$(git show "HEAD:$app_manifest" | jq -r .version)"
[[ "$(git show HEAD:apps/shs_energy/config.yaml | sed -n 's/^version: *//p')" == "$app_version" ]] \
  || fail "apps/shs_energy/config.yaml and $app_manifest name different app versions"
sha="$(git rev-parse HEAD)"
changed="$(git diff --name-only origin/main HEAD)"
changes() { grep -qE "$1" <<<"$changed"; }

if [[ "$(git rev-parse origin/main)" == "$sha" ]]; then
  echo "origin/main is already up to date."
else
  # Each workflow refuses, or silently overwrites, a version it has published.
  if changes '^custom_components/' && published "$version"; then
    fail "integration $version is already published, so the Beta workflow would reject this push; run scripts/bump.sh beta --commit"
  fi
  if changes "$app_paths" && app_published "$app_version"; then
    fail "app $app_version is already published and a published version is never rebuilt; bump it in $app_manifest, web/package-lock.json, apps/shs_energy/config.yaml and apps/shs_energy/Dockerfile"
  fi
  if changes "$beta_paths" && ! published "$version" && [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[0-9A-Za-z.-]+$ ]]; then
    fail "$version is not a pre-release, so the Beta workflow would reject it; run scripts/bump.sh beta --commit"
  fi
  if changes "$beta_paths" && published "$version" && ! changes "$app_paths"; then
    # Only the integration's tests changed, so these wait for the next bump
    # instead of failing the Beta workflow's version check.
    echo "Not pushing: integration $version is already published and these change nothing that installs."
    git log --oneline origin/main..HEAD
  else
    if changes "$beta_paths" && published "$version"; then
      echo "The Beta workflow will fail its version check on this push; only the integration's tests changed, so that is harmless."
    fi
    git log --oneline origin/main..HEAD
    git push origin main
  fi
fi

step "Waiting for the Beta workflow to publish integration $version"
if published "$version"; then
  echo "$version is already published."
else
  wait_for_workflow beta.yml Beta
  published "$version" || fail "the Beta workflow finished without publishing $version"
fi

step "Waiting for the SHS app workflow to publish app $app_version"
if app_published "$app_version"; then
  echo "$app_version is already published."
else
  wait_for_workflow app.yml "SHS app"
  app_published "$app_version" || fail "the SHS app workflow finished without publishing $app_version"
fi

step "Connecting to Home Assistant at root@$ha_host:$ha_port"
remote="set -euo pipefail
$(declare -f step fail supervisor_api install_on_home_assistant update_app_on_home_assistant)
install_on_home_assistant \"\$1\" \"\$2\" \"\$3\"
update_app_on_home_assistant \"\$4\" \"\$5\""
status=0
ssh "${ssh_options[@]}" "root@$ha_host" \
  docker exec -i "$api_container" bash -s -- "$version" "$update_entity" "$domain" "$app_version" "$app_slug" \
  <<<"$remote" || status=$?
if (( status == 255 )); then
  fail "could not SSH to root@$ha_host on port $ha_port, the host login that the HassOS SSH port 22222 Configurator add-on sets up"
fi
(( status == 0 )) || exit "$status"

step "Deployed integration $version and app $app_version"
