# House import safety review — 17 October 2026

The supplied capture exposed real command-admission defects, so it was not justified to describe the previous controls as fuse-proof. A code review and tests cannot assign a defensible numerical probability of a fuse trip. The changes below close specific reproducible software paths; they do not establish an absolute electrical protection guarantee.

The scope is total house import, as requested. The captured website cap was 15,000 W. The retired local 13,200 W setting was an inverter figure, not authority for the house cap. The tariff endpoint now resolves the website's chosen limit against the house fuse using the same helper as planning. The app removes the obsolete local field and saved value, and passes that resolved authority to HA separately from the plan. Missing authority admits no increases. Export configuration remains independent.

## Defects corrected

The separate Verification fix is commit `c5154e1`. A charger current setting while its switch was off incorrectly waited for battery headroom. Battery relief also failed when measured charging was a watt above the preceding register setting. Verification still restores captured settings, with a fresh stop-before-current check if the charger changes during handover.

The subsequent hardening closes these paths:

- Previously accepted EV or battery demand could disappear while native registers and power meters still showed their earlier values. Desired ownership values were not reliable evidence of accepted physical effects.
- A stale high register could make a new increase look like a reduction after a lower command had already been accepted.
- A mapped relay's added load was omitted from the final house check. Unsupported automatic battery modes also cannot be interpreted as idle; they reserve the native rating.
- An equal stale reading could skip a lowering command despite an outstanding different command.
- Changing actuator mappings could union two independent loads with `max`, instead of adding them.
- A recoverable input error during command description could be mistaken for a permanent journal failure.

## One physical admission boundary

All production native writes use the existing `NativeExecutor` and durable transport. The final synchronous authorization checks the current owner, policy, native metadata and import budget after durable preparation, immediately before the service. The existing battery grant and commissioned transition checks remain in place.

Gross house demand excludes battery charging. Known device demand is unioned with its native/commanded bound; independent devices are added. Each physical actuator set has its own partition. A shared or replaced measurement cannot be subtracted twice. Solar production and battery discharge provide no discretionary import credit, which deliberately makes admission conservative when the plan relies on those sources.

Preparing a command stores its possible demand in the existing gateway SQLite transaction. Successful service completion does not release it. A dispatch marker on the same FIFO worker separates pre-dispatch reports from actual response evidence without source-timestamp ordering or a periodic-report requirement. Native acknowledgement plus physical response settle successful commands. Never-observed uncertain higher effects remain reserved across lower commands and restarts. A lowering command must be nonincreasing against both native and preceding commanded settings to bypass the budget.

EV bounds use configured current controls and the existing AC profile. Battery bounds use the native charging envelope and installation conversion. Other mapped equipment uses its Power sensor's greatest recorded electrical watts, or the existing literal Power watts. The user required history-derived peaks and removal of the separate maximum-power field on 17 October 2026. HA aggregates all available long- and short-term Recorder maxima in watts, then retains increasing peaks durably with source observations. This is observed evidence, not a guaranteed physical rating; a later higher reading raises the retained peak. It is never a mean, guessed margin or age-based bound. Missing positive evidence holds only that device's demand increase and identifies its sensor; it does not become a missing configuration field. Stopping remains possible, and house consumption still links to Energy.

Battery charging also sizes its requested DC rate to this gross import budget,
without solar credit. Each refresh reads HA's existing native and durable demand
reservations for the rest of the house, excluding only this battery's current
actuator set so its proposed charging envelope is not counted twice. The runtime
includes that demand in its physical frame and reduces or raises charging within
the accepted plan as capacity changes. This read is advisory: the unchanged
runtime, preparation and final native dispatch checks still govern every write,
including when demand changes after sizing or an older battery write is unresolved.

Multiple energy counters mapped to one EV current control describe the same
physical load. Admission resolves that control once; if the aliases retain
different power sources, the explicitly admitted battery-capture binding selects
the physical meter. Distinct controls or multiple capture meters remain ambiguous
and are refused. Native import accounting and command envelopes share this
resolution, so retired counter mappings cannot independently block the battery's
capacity query or count the charger more than once.

## Complexity and practical limits

The old EV/battery coordination was spread between the app's reservations, native battery transitions and HA's final device gate. The aggregate check is now shared at the common transport boundary, with one durable demand ledger in the existing journal. The ledger adds necessary state for outstanding effects; replacing it with a meter-only comparison would recreate the lag bugs. The battery's native multi-register transition state machine remains necessary and is not replaced by another executor.

The remaining limits matter:

- The check governs SHS commands. An unrelated appliance or manual change can increase demand after admission, before a new observation arrives.
- Incorrect ratings, conversion data or measurements can invalidate the accounting. Equipment that ignores, reorders or indefinitely delays writes cannot be proved safe by a successful HA service response.
- A never-observed uncertain command, or an EV without a physical power source, can conservatively retain capacity and block further increases. It is not erased by a timeout or restart.
- Existing user-required control continuity holds the last settings through app/socket loss, faults and missing plans. This work does not introduce an independent load-shedding watchdog during those gaps. A held charging setting cannot follow later house-load changes during such a gap.

Consequently these changes strengthen software admission substantially, but SHS alone still cannot promise that the house limit is never exceeded. This review does not verify the installation's independent protection or equipment behaviour on hardware.

## Validation

Regressions exercise delayed EV and battery readings, stale-high reductions, restart loading, uncertain effects, dispatch ordering, actuator replacement, live house-load changes during prepare, recovery from unavailable input, rated relay demand, setup correction and unchanged Verification handover semantics. The full integration/app suites and browser tests accompany the change. Private uploaded captures are not committed.
