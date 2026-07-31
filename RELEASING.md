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
changes since the most recent published release, creates the matching tag, and
publishes the GitHub Release. It stops without publishing if the tests fail, the
version already exists, the Copilot secret is unavailable, or the generated
notes are empty.
