# Home Assistant entity commands

The controller chooses the requested settings. The HA transport checks the current
request and user permission after acquiring the command lock, calls the mapped
service with `blocking=True`, and returns when that service completes. Number
values are converted to the configured entity's W or kW unit. Verification records
the proposed call without sending it.

A successful service result advances the battery settings sequence once. It does
not credit delivered energy or manufacture a physical observation. The next
assignment uses acknowledged settings, including when a new request reverses a
completed assignment before HA has published its state. Normal observations retire
acknowledged settings once they report the final values; later external changes
can then produce a new sequence. A delayed or duplicate result cannot advance a
different request.

There is no separate post-write confirmation event, forced Sigen coordinator
refresh, source-timestamp ordering test, or 100 W power-settling gate. An unchanged
HA setting does not expire. Freshness applies to actual power and SOC measurements.
Physical reservations include measured battery power as well as configured native
limits, so an optimistic HA setting cannot make measured consumption disappear.

Successful commands are distinct from actual battery charging, discharging and
metered energy. These measurements and empirical loss estimates continue through
the existing accounting path. Genuine service exceptions remain visible and retain
uncertainty; saved issued commands are reconciled on restart, never blindly replayed.
The command journal, native value bounds, user overrides and exclusive writer remain.

Pool, EV and other mapped entity calls also return on service completion rather
than polling for an exact state match. Their separate device workflows still own
any explicitly required physical-response checks. Pool heating remains switch-only.

## Design decision

The architect review compared Claude's observation-gated settlement with Codex's
service-completion sequence. The latter is the base because a completed HA service
must not wait on another observation to permit the next setting. Both candidates
supported removing duplicate pre-lock checks and the second observation writer.
The implementation keeps the existing persisted sequence/attempt representation;
acknowledged settings and physical observations remain separate. It accepts HA's
service contract for command completion, while energy delivery remains measured.
