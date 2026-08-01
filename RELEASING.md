# Releasing

The integration version in
`custom_components/shs_energy/manifest.json` is the release source of truth. Do
not create release tags manually.

## One-time setup

Add a repository Actions secret named `RELEASE_TOKEN`. Its value must be a
fine-grained GitHub personal access token with the **Copilot Requests**
permission.

## Publish a release

1. Update the manifest version and merge the release commit into the default
   branch.
2. Open **Actions → Release → Run workflow** and select the default branch.

The workflow validates the version, runs the integration tests, summarizes the
changes since the most recent published *stable* release, creates the matching
tag, and publishes the GitHub Release. It stops without publishing if the tests
fail, the version already exists, the Copilot secret is unavailable, or the
generated notes are empty.

## Release candidates

Pushing to the default branch installs nothing — HACS only ever offers
published releases. To get an untested build onto a real Home Assistant, cut a
release candidate instead.

Give the manifest a pre-release suffix, e.g. `0.4.0-beta.1`, and run the
workflow exactly as above. Any version containing `-` is published as a GitHub
pre-release rather than the stable one, so ordinary installs keep seeing the
last stable version.

**Use `-beta.N`, not `-rc.N`.** The workflow publishes either as a GitHub
pre-release, but HACS also asks `AwesomeVersion` whether a version is a beta,
and that only recognises `beta`/`b` as a marker — `0.4.0-rc.1` reports
`beta=False` and risks being offered to installs that never opted in.
`0.4.0-beta.1` reports `beta=True` and still sorts below `0.4.0`.

To install one, enable beta versions for this repository in HACS once:

> HACS → Smart Home Solutions Energy → ⋮ → **Redownload** → toggle **Show beta
> versions**, then pick the `-beta` version.

Restart Home Assistant afterwards; `custom_components` changes never apply
without it.

Iterate with `-beta.2`, `-beta.3`, … as needed. When a candidate is good,
promote it by setting the manifest to the bare version (`0.4.0`) and running the
workflow again — no new commits are required, and that stable release is what
every non-beta install then receives.
