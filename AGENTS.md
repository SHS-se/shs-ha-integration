# Highest priority: planning and execution keep running

Planning on the website and execution in the Home Assistant app are the highest
priority features of this system. Neither may stop unless a critical error makes it
impossible to perform its function, such as the database being unavailable.

- **Planning** must be durable, recover from errors automatically, and tolerate data
  gaps and unavailable sensors. Leaving an individual device out of a plan is
  acceptable if the user is shown a warning. Stopping the whole planner is not.
- **Execution** must be durable, recover from errors automatically, and tolerate data
  gaps and unavailable sensors. Not executing an individual device is acceptable if
  the user is shown a warning. Stopping all execution is not.

A general preference for failing fast does not apply to these two paths. A device
that execution cannot act on still follows Control continuity below: it holds the
last setting SHS sent.

# Other repository

The website, planner and backend are in the separate `smart-home-solutions`
repository, checked out beside this one as `smart-home-solutions-t-by`. Before
changing anything there, read its `AGENTS.md` and follow it.

# Working in this checkout

Other sessions and the user work in this tree too. Before editing or committing,
check the branch and `git status --porcelain`. If not on `main` with a clean tree,
switch to `main`. If not on `main` with staged or uncommitted changes, stop and
tell the user instead of switching, stashing or committing; they may be mid-merge.

Stage files by name, never with `git add -A` or `git add .`. Account for every
line of `git status` before committing; an untracked file you did not write is not
yours to add. Check `git diff --cached --stat` for changes larger than you made.

Never use `git stash` to compare against `HEAD`; an interrupted command leaves the
work stranded. Read the test, inspect the history or use a temporary worktree.

# Answering questions about the setup

For what a home has, what is configured or whether prerequisites are met, query
the Home Assistant connector and search earlier sessions when those tools are
available. Do not answer from source or vendor documentation alone. Verify entity
IDs, units and current values before recommending anything built on them.

# Naming and navigation

Call the Home Assistant side "the app" when talking to the user, not "the
integration"; the repository name predates the conversion. Do not give click paths
from section ids, `"tab"` values or card titles in `configuration_fields.py`, which
are not navigation. Check `TABS` in `web/src/configuration-editor.js` or the running
app, or say the path is unverified. What the planner needs to know about the home
is asked in the website home profile, not added as an app setting.

# Runtime checkpoint

`home_runtime_checkpoint.py` persists validated dataclasses, and decoding runs every
`__post_init__` against data written by the previous version. Changing validation on
a dataclass reachable from `HomeState` makes setup fail outright unless the checkpoint
migrates it. Bump its `schema_version` and add a one-way, idempotent upgrade beside
the existing ones, rewriting the stored record rather than discarding it. Reproduce
the failure first by decoding JSON in the old shape.

# Integration changes

Follow [the constraint requirements](docs/constraint-requirements.md). Do not
invent or restore arbitrary validity constraints, including prediction bounds
or source-timestamp event ordering, unless the user explicitly requires them.

Whenever integration code changes, bump `custom_components/shs_energy/manifest.json`
with `bash scripts/bump.sh beta` and include the version bump in the same commit.
The Beta workflow publishes the manifest version and fails if that version has
already been published. Documentation-only changes do not require a bump.

Follow `RELEASING.md`; do not create release tags manually.

The SHS app bundles `custom_components/shs_energy/shs_core` and `shs_wire`.
When either changes, bump the app version in `web/package.json`,
`web/package-lock.json`, `apps/shs_energy/config.yaml` and its Dockerfile in the
same commit. The app version bump starts its publication workflow; publishing
only the HACS integration does not update the running app core.

# Control continuity

Follow [control continuity](docs/control-continuity.md) for every device, the
battery included, and read its History section before touching release logic.
Only an explicit control-mode, exclusion or override setting may change a
device's settings:

- A restart, a lost app or socket, a settings or metadata change, a missing or
  expired plan, stale readings and faults all hold the last setting SHS sent. Do
  not add a handback, fallback or "safe default" write for any of them, in the
  app, the core or the Home Assistant gateway.
- Switching from Controlling to Verification stops control at the device's
  current settings, including the battery. Cancel commands that can still be
  cancelled and retire any pending handover. Explicit exclusion and manual
  override can still request a baseline handover as described in the contract.

Both halves have been lost before through unrelated work whose tests were
rewritten to match. A change to when a device is released needs the user's
explicit requirement. If a change makes `tests/test_control_continuity.py`,
`tests/test_battery_continuity.py`, `tests/test_pool_switch_gap.py` or the
Verification handover tests fail, stop and ask instead of editing them.

# Configuration UX

For configuration, readiness, status, or controller-error changes, follow
[the configuration error UX contract](docs/configuration-error-ux.md).
Known setup failures must enumerate their fields, highlight their real editors,
and link directly to those fields; cross-section requirements must affect readiness
and required-field visibility. Add regression coverage for the full correction path.
