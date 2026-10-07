# Production and test planning

The app publishes to independently paired production and test backends. One
shared API client and contract validator serve both. Each Household owns its
cloud record, watermarks, pending submissions/jobs, cached choices and plan.
Only the selected Household has physical control authority. There is one
scheduled controller, battery runtime, execution journal and gateway writer.

The architecture review compared independent Claude and Codex designs. The
Claude constructor-time authority and restart approach was chosen because it
keeps existing controller references stable throughout a running engine and
uses the established restart continuity path. The Codex findings about battery
execution generations and observer acknowledgements are incorporated: observers
cannot capture execution feedback, advance the physical journal, admit choices,
sample physical battery state, report execution or acknowledge executable plans.
Extracting the entire cloud layer from Household would be a larger migration.

Pairing persists a credential for one known backend. Selection requires a fresh
status, active subscription, valid ready candidate plan and equal participating
physical owners/control methods. Revision identities can transfer between
websites; device modes cannot change. The durable selected source then triggers
an engine restart, with release disabled and physical journals retained. Startup
completes admission transfer idempotently before any controller starts. The
selection remains durably unsettled until the selected backend accepts a fresh
plan. Startup explicitly requests that replan, preserving pending submissions
and jobs across restarts; background refresh recommendations cannot complete it.

Observer plans omit the optional battery execution feedback under the same
contract. After selection the battery holds its last setting until a freshly
requested plan returns with its physical execution generation. No automatic
backend switch or baseline handover occurs on failure. Different device
participation blocks selection and needs an explicit website correction.

The original single backend credential and cloud store are adopted exactly once
into the matching environment. Unknown endpoints or missing adopted records
fail explicitly. Physical stores remain shared. Credential values stay local
and never appear in the UI or diagnostics. Tests cover independent delivery,
observer authority, selection gates, admission transfer and restart persistence;
the existing continuity and Verification tests remain authoritative.
