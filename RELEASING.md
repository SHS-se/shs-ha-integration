# Releasing

The integration version in `custom_components/shs_energy/manifest.json` is the
release source of truth. Do not create release tags manually.

Two workflows publish from that number, and both run the full test suite first:

| Workflow    | Runs on                     | Publishes                  | Visible to                       |
| ----------- | --------------------------- | -------------------------- | -------------------------------- |
| **Beta**    | every push to `main`        | `0.4.0-beta.N` pre-release | only HACS installs that opted in |
| **Release** | manual run, when you say so | `0.4.0` stable             | everyone                         |

Neither invents a version: both publish exactly what the manifest names, so the
number is bumped locally as part of the change.

## One-time setup

Add a repository Actions secret named `RELEASE_TOKEN`. Its value must be a
fine-grained GitHub personal access token with the **Copilot Requests**
permission. Only **Release** needs it; **Beta** uses GitHub's generated notes so
throwaway builds never depend on it.

## Cut a beta to test on real hardware

**Bump the version in the same commit as the change**, then push — the Beta
workflow publishes whatever the manifest names:

```bash
scripts/bump.sh beta --commit   # 0.4.0-beta.3 -> 0.4.0-beta.4
```

The workflow never edits the manifest. The version belongs to the commit that
changed the code, so nothing is auto-incremented and no bot commits land on
`main`. If the version was not bumped, the run fails with *Version not bumped*
rather than publishing a second build under a number someone already installed
— HACS compares version strings, so reusing one leaves that install thinking it
is up to date.

Pushes that only touch `.md` files are skipped, and the workflow ignores the
Release workflow's own `Release <version>` commits so publishing cannot loop.
You can also start a run by hand from **Actions → Beta → Run workflow**.

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
scripts/bump.sh minor --commit   # 0.4.0 -> 0.5.0
scripts/bump.sh beta --commit    # 0.5.0 -> 0.5.0-beta.1
```

`bump.sh` takes `beta`, `release` (drop the suffix), `major`, `minor`, `patch`,
or an explicit `X.Y.Z`, and refuses a no-op.
