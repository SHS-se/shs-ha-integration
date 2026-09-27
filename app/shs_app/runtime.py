"""Receipt replay adapts historical accounting to a newly resumed live host."""
from shs_core.battery_runtime import BatteryRuntime
from shs_core.runtime_json import Records
from shs_core.home_runtime import TRACE_RETENTION_MS


class AppBatteryRuntime(BatteryRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._durable_state = None

    async def _save_state(self, state, processing):
        # An empty counter sub-event changes only the in-memory replay cursor.
        # The complete receipt still commits its source mirror and cursor. A
        # crash here resumes the preceding receipt and safely repeats the no-op.
        if processing and not processing['complete'] and state is self._durable_state:
            return
        await super()._save_state(state, processing)
        self._durable_state = state

    def snapshot(self, *, include_evidence=False):
        # Operational facts remain in indexed storage. A routine diagnostics
        # download must not reconstruct or serialize their lifetime archive on
        # the controller loop; it contains the current account and recent traces.
        value = super().snapshot(include_evidence=False)
        if include_evidence:
            traces = self.host.state.execution.traces if self.host else ()
            cutoff = self.now() - TRACE_RETENTION_MS
            recent = tuple(row for row in traces if row.at_ms >= cutoff)
            value.update(execution_traces=Records(recent),
                execution_trace_retention={'retained': len(recent), 'max_age_days': 3},
                accounting_evidence_scope='Current accounting state. Historical operational facts remain in the database and are not included in this diagnostic download.')
        return value

    async def _observe(self, group_id):
        events = await super()._observe(group_id)
        # HomeHost explicitly starts a new live-feedback epoch on resume.
        # Historical counters still pass through consume_receipt unchanged;
        # observations from before that epoch cannot authorize native control.
        if self.host and any(event.observed.observation.at_ms < self.host.state.resume_after_ms for event in events):
            return ()
        return events
