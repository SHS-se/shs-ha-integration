# Runtime recovery fixes

## Later design decision — 15 September 2026

Preserve the dated implementation/rollout evidence below. The agreed participation ownership, metadata exclusion, graph partition and explicit battery supply scope supersede conflicting target requirements; this older record is not an instruction to deploy the replacement.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Implementation scope: the four reproduced defects from the 14 September
[architecture review](controller-architecture-review.md). The completed independent
Opus Max/Codex designs are reused; production policy replacement and commissioning
are outside this change.

## Usage and shape

```python
state, effects = reduce_home(state, event, now_ms)
# NeedTransition includes a unique job token and deadline. The worker returns
# Proposed(..., token=job.token) or TransitionFailed(group, token, outcome, reason).
# Tick retries lost work; stale tokens cannot supply a new transition.
state, effects = reduce_home(state, LedgerPruned(cut_ms), now_ms)
# Settlement and pruning share one checkpoint; policy expiry is not required.
```

One reducer still owns all changes. Pending transition jobs have bounded deadlines
and tokened completion; retryable failure gets capped exponential pacing, while an
unsupported result remains visible until relevant inputs change. Unsent retry
preparations are cancelled atomically when their sequence advances or is invalidated.
Already-issued attempts retain their physical uncertainty.

Per-policy accounting retains its original watermark and one settled prefix/cursor
per meter. Pruning settles only through each stream's actual retained counter
anchor, never through an unobserved wall-clock tail. Replacement combines each
settled prefix and retained tail once. A slow or absent stream cannot block pruning
an unrelated stream or fabricate zero delivery.

Overload relief requires a configured finite adapter rule naming exact controls,
native guards, before/during/settled electrical envelopes and evidence. A step can
reference that rule; it cannot invent one. Admission requires fresh frame/readback,
no unresolved issued effects in that group, no increased directional envelope and
strict improvement in a violated direction. Dispatch repeats those checks.
Reservations are retained until physical confirmation; no other group spends the
requested reduction. This supports the declared aggregate electrical profile only,
not unmodelled per-phase or native supply effects.

## Synthesis and tradeoffs

Use the review's single-owner design, accounting settlement and identified worker
jobs. Retain exact preparation acknowledgements. Reject clearing an expired policy
to free history, interpreting a smaller register as relief, or bypassing freshness.
We accept exact finite relief rules in exchange for explicit supported scope.
We accept bounded per-stream summaries in exchange for retaining provenance through
long compiler outages. The offline checkpoint moves to version 4 without migration.

## Verification

Regress the four original failures, then test stale/failed work, restart and late
replies; multiple retention windows with asynchronous meters, resets and partial
intervals; and relief refusal for stale, unapproved or worsening transitions.
Every persisted state must pass the closed checkpoint codec. Existing suites and
committed replay traces remain required. No live device evidence is inferred.

Implementation completed in `0.8.0-beta.89`. Validation passes 583 Python tests,
including 18 new recovery regressions, and 58 frontend tests. Generated provider
fixtures and the three updated schema-4 replay CLIs pass. Independent review found
no further defects in this scope, including randomized settlement comparisons and
runtime checkpoint checks. No live settings or deployment were changed.

The remaining deployment work is documented in the architecture review: executable
native-response policy coverage, mixed-mode execution/coexistence, live ordered
effect ports, and hardware commissioning. These fixes do not make the replacement
controller ready for battery enablement.
