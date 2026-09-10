# Agreement transport v1

`POST control-agreement` uses the existing HA API success/error envelope. All
requests carry `home_id`. Device `sync` uses the hashed device-token credential;
portal `read`, `edit`, and `create` use the authenticated user's home access.
There is no public plan-publication or enablement action. The paired device that
first reports bindings owns that home revision space; a different token receives
`binding_owner_conflict` until a reviewed server-side ownership transfer.

| Action | Additional request fields | Result |
| --- | --- | --- |
| `read` | None | Server state and derived synchronization/authority status |
| `edit` | `expected_revision`, complete `definition` with every control's `local` omitted | Atomic complete edit; server assigns revisions; stale editor receives 409 |
| `create` | Same as edit; exactly one new control with `control_id: null` | Server assigns UUIDv4 to that control, then commits the complete edit |
| `sync` | `schema_version: 1`, `bindings`, `acknowledged`, `accepted_epoch`, `plan_id`, `active` | Desired definition, server acknowledgement, current plan, optional lease and status |

A browser edit includes the full home definition: `kind`, `schema_version`,
`home_id`, `sources` and `controls`. Existing IDs remain stable. Related objective,
association, model and inclusion edits are validated together. The server owns
`desired.revision` and `desired.energy_model.revision`; supplied values do not choose
those revisions. `expected_revision` refers to the home `edit_revision`. Creation
also requires that revision, so retrying an uncertain create cannot create twins.

Each binding advertisement contains `{control_id, contract, local, ready}` and
may include `interface_summary`, keyed by semantic role, with only descriptive
`manufacturer`, `model` and `platform` strings (or null). Metadata changes do not
invalidate a plan or increment the binding revision.
`local` is the public semantic projection defined in `schema.json`. It has no entity
or registry IDs. HA reports its persisted binding revision, and sets `ready` false
if capability, availability or ownership checks fail. Relay timings and permission
inhibit limits use the same reviewed limits as local setup. Other generic output
ranges include their unit. Pool includes only customer request/feedback/water roles.

`acknowledged` maps control IDs to accepted revision tuples. HA sends only tuples
already durably saved locally. `accepted_epoch` acknowledges receipt of the complete
household definition, including removals. `plan_id` is null until HA receives the
validated plan. `active` is null unless a hardware adapter has explicitly reported
operation; such a report contains `plan_id`, the complete `accepted` map and
`observed_at_utc`. Publication, lease renewal and HTTP success do not manufacture it.

A sync response includes `schema_version`, `home_id`, `server_time_utc`,
`edit_revision`, `epoch`, `definition`, `acknowledged`, `plan`, `lease` and `status`.
A lease is `{plan_id, epoch, issued_at_utc, expires_at_utc}`. UTC strings end in `Z`.
Its duration is at most 120 seconds and its expiry cannot exceed plan expiry.
Only the current plan and fully acknowledged household epoch can renew authority.
The polling interval is 15 seconds, independent of measurement uploads.

Synchronization is `pending` until HA's persisted acknowledgement of the complete
current epoch is received. Authority is `leased`, `expired`, or
`change_pending_receipt_or_expiry`. The last state deliberately preserves the last
issued expiry after a remote edit: it cannot promise immediate physical stop.
`active` is separately timestamped; `active_fresh` is false after 120 seconds without
a new report. A current active report is still a report, not a new authority grant.

Persistence, edit, acknowledgement and publication all pass through the same home
CAS boundary. Solver snapshots carry `epoch`, accepted tuples and
`publication_revision`; publication rejects a changed snapshot or a newer completed
solver. Every included control must have one command in the same household plan.
Presentation-only edits do not invalidate the execution snapshot. All other related
changes conservatively invalidate the entire household dependency group.

## Step-4 planner publication

The optimizer emits explicit instructions in each planned slot. The current
binding quarter supplies the v1 plan, bounded by that quarter and forecast expiry.
Ceilings are allocations, separate from delivery forecasts. An empty command list
is valid when no composed controls are included; publication requires exactly the
set of included control IDs.

The service-only `publish_control_optimisation` transaction compares the agreement
version and fixed-plan generation revision, then writes the agreement publication
and household optimization row together. The publication and household plan IDs
must match. No public endpoint can publish commands or enable controls.

Pool `energy_model` may carry reviewed `running_power_w`, `thermal_conversion`
(effective heat/electricity ratio including auxiliaries), and
`thermal_loss_kw_per_c` estimates with provenance. All three and attributable meter
history are required by the pool planner. Live observations are not model ratings.
Current customer feedback can report limited deferral or unavailable heat for
replanning; it neither grants authority nor changes desired/model revisions.
