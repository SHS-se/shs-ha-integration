# Operational failure reporting design

## Problem
Observable failures in input collection, upload, planning, delivery, acceptance, acknowledgement and execution must be prominent across all screens. Existing aggregate diagnostic strings conflate their recovery. Controls and source-age admission must remain unchanged.

## Usage
Household records `self._pipeline_error('upload', str(error))` at failed upload, then `self._pipeline_error('upload', None)` only after its own accepted response. Server planning, plan delivery, acceptance and acknowledgement use separate keys. A telemetry success cannot clear them. Existing `_attention` owns these items; structured setup attention retains its editors. AppEngine records host/task failures in its own dictionary and Dashboard.payload merges live host records with the current owner's attention and retained execution snapshot.

## Shape
A pure shs_core/operational_attention.py builds stage items and derives execution items from explicit statuses/codes. Household's existing attention map owns stage state. A stage helper handles assignment/removal and listener notification without inventing HA repair translations. Existing push/runtime locks and receipt identity checks govern recovery; serialize cloud refresh to prevent stale concurrent completion. App task callbacks require the latest task identity for their role. No event ledger, persistence, new controller, source-age gate, fallback write, or release rule.

Dashboard publishes local state before HA projection. Its payload reads current app failures even if projection/build/task fails. A shared React notice lives above every screen; existing Settings renders the same owner attention with its correction actions. Browser tests prove cross-screen visibility, field navigation, failed execution, retained plan and independent recovery.

## Synthesis
Base: Codex's existing-attention alternative, chosen because push/report/listener operations already have serialized owners or durable receipt identities. Adapt Claude's independent host overlay, projection ordering and stage-by-stage recovery audit. Reject its additional generic fault map, receipt fields, event sequence/mark API and legacy-error-property migration: these add state and affect replan timing without being necessary for visibility. Keep legacy attempt strings as diagnostics, not as the source for active UI issues. Both candidates independently identified the same publication ordering defect.

## Tradeoffs
We accept explicit recovery calls at known success boundaries in exchange for reusing ownership and existing correction records. We accept transient operational state being revalidated after a restart in exchange for avoiding a new persisted incident schema. Logs remain historical evidence; active notices have no dismiss action.

## Risks and validation
Do not clear plan rejection on absent response or unrelated successful upload. Do not clear source problems before their same reader succeeds. A stale poll cannot mutate attention. Report real command blocks with producer-provided flags, not reason parsing; healthy planned-off/verification work is quiet. Protected continuity/handover tests stay unchanged. First implement stage item construction and pipeline regression cases, then host overlay and global browser notice.

## Verification

Validated 15 October 2026: 1,183 core tests, 147 app tests against a freshly built companion bundle, 89 editor tests, and 46 desktop/mobile browser tests pass. TypeScript/Vite build, app packaging checks and whitespace checks pass. Protected continuity and Verification handover tests are unchanged and pass. Browser coverage includes every screen, dark mode, retained schedules, startup failure without a snapshot, exact field correction and source inspection, and independent failure recovery. Integration version is 0.9.0-beta.109; app version is 0.1.0-beta.71. These changes have not been deployed.
