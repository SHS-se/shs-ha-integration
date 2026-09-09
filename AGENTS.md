# Integration changes

Whenever integration code changes, bump `custom_components/shs_energy/manifest.json`
with `bash scripts/bump.sh beta` and include the version bump in the same commit.
The Beta workflow publishes the manifest version and fails if that version has
already been published. Documentation-only changes do not require a bump.

Follow `RELEASING.md`; do not create release tags manually.
