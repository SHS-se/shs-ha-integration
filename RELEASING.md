# Releasing

The integration version in `custom_components/shs_energy/manifest.json` is the
release source of truth. Do not create release tags manually.

Two workflows publish from that number, and both run the full test suite first:

| Workflow    | Runs on                    | Publishes                  | Visible to                       |
| ----------- | -------------------------- | -------------------------- | -------------------------------- |
| **Beta**    | every push to `main`       | `0.4.0-beta.N` pre-release | only HACS installs that opted in |
| **Release** | manual run, when you say so | `0.4.0` stable             | everyone                         |

## One-time setup

Add a repository Actions secret named `RELEASE_TOKEN`. Its value must be a
fine-grained GitHub personal access token with the **Copilot Requests**
permission. Only **Release** needs it; **Beta** uses GitHub's generated notes so
throwaway builds never depend on it.

## Cut a beta to test on real hardware

Nothing to run — **every push to `main` publishes one**, so your loop is just
push, install, test, repeat. (You can also start one by hand from
**Actions → Beta → Run workflow**.)

It works out the next number itself: it takes the base version from the
manifest, finds the highest `base-beta.N` tag already published, and publishes
the next one. From `0.4.0` or `0.4.0-beta.3` it targets `0.4.0`, so the first
run gives `0.4.0-beta.1` and each later run increments. It then writes that
version back into the manifest and commits it, because Home Assistant reads the
version from the manifest and would otherwise disagree with HACS.

Pushes that only touch `.md` files are skipped, and the workflow ignores its own
`Release <version>` commits so publishing can never loop.

Each beta is a real, immutable GitHub release. Versions are never reused —
HACS compares version strings, so republishing the same number would leave an
install that already has it thinking it is up to date.

To install one, enable beta versions for this repository in HACS once:

> HACS → Smart Home Solutions Energy → ⋮ → **Redownload** → toggle **Show beta
> versions**, then pick the `-beta` version.

Restart Home Assistant afterwards; `custom_components` changes never apply
without it.

**Why `beta` and not `rc`:** HACS asks `AwesomeVersion` whether a version is a
beta, and it only recognises `beta`/`b` as a marker. `0.4.0-rc.1` reports
`beta=False` and risks reaching installs that never opted in.

## Publish the stable release

Open **Actions → Release → Run workflow** on the default branch.

It publishes the base version, dropping any beta suffix — after testing
`0.4.0-beta.3` the release is simply `0.4.0`, with no manifest edit needed. It
validates the version, summarizes the changes since the most recent published
stable release, rewrites the manifest to the bare version and commits it,
creates the tag, and publishes the release. It stops without publishing if the
tests fail, the version already exists, the Copilot secret is unavailable, or
the generated notes are empty.

## Start the next series

After `0.4.0` ships, decide what comes next:

```bash
scripts/bump.sh minor --commit
```

`major`, `minor`, `patch`, or an explicit `X.Y.Z`. Push it, and the next **Beta**
run produces `0.5.0-beta.1`.
