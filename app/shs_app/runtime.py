"""Receipt replay adapts historical accounting to a newly resumed live host."""
from shs_core.battery_runtime import BatteryRuntime, ACCOUNTING_REUSE_MS
from shs_core.runtime_json import Records
from shs_core.home_runtime import TRACE_RETENTION_MS, Tick


class AppBatteryRuntime(BatteryRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._durable_state = None
        self._accounting_ready_at = None
        self._ingested = None
        self._last_committed_at = self.now()

    def received_checkpoint(self):
        values = [value for value in (self._ingested, self._processing) if value is not None]
        return max(values, key=lambda value: (value['receipt'], value['sub_event'], value['complete'])) if values else None

    async def ingest_receipt(self, identity, receipt):
        """Accept evidence promptly; ordinary decisions belong to the 5s clock."""
        row = receipt['payload']
        recovering = self._observation_error is not None
        async def receive(event, checkpoint):
            if event is None and not checkpoint['complete']:
                return
            if self.host is None:
                if receipt['kind'] != 'observation':
                    await self._bootstrap_received(checkpoint)
                return
            overrides = {value for key,value in self.controller.options().items() if key.endswith('control_override_entity')}
            urgent = (receipt['kind'] != 'observation'
                      or (row['entity_id'] in set(self._control_entities()) | overrides and row.get('kind') == 'state_change')
                      or recovering and event is not None)
            receiver = self.host.accept_received if urgent else self.host.ingest_received
            await receiver(event, checkpoint)
        self._ingested = await self._consume_receipt(identity, receipt, receive, self.received_checkpoint)
        if recovering and self._observation_error is None and self.host is not None:
            await self.host.accept(Tick())

    async def commit_evidence(self):
        async with self._lock:
            if self._ingested is not None and self._ingested != self._processing and self.now()-self._last_committed_at >= 5000:
                if self.host is None:
                    await self._bootstrap_received(self._ingested)
                else:
                    await self.host.commit_evidence()

    async def _bootstrap_received(self,checkpoint):
        await super()._bootstrap_received(checkpoint)
        self._last_committed_at=self.now()

    async def _receipt_observation(self,row):
        # A meter/reference delta cannot change any complete-frame input. Keep
        # its evidence, without cloning and validating the same live capture.
        if self.host.state.authority is None or row['entity_id'] not in self.capture_entities():
            return ()
        return await super()._receipt_observation(row)

    def _accounting(self, account, include_evidence):
        cached = self._live_accounting
        if (not include_evidence and cached is not None and cached[0] is account
                and self._accounting_ready_at is not None
                and 0 <= self.now()-self._accounting_ready_at < ACCOUNTING_REUSE_MS):
            return cached[1], cached[2]
        value = super()._accounting(account, include_evidence)
        if not include_evidence:
            # The result retains its actual sampled time. Its reuse period starts
            # when calculation finishes, so a slow read can still be shared by
            # the status, entity and mode projections in the same refresh.
            self._accounting_ready_at = self.now()
        return value

    async def _save_state(self, state, processing):
        # An empty counter sub-event changes only the in-memory replay cursor.
        # The complete receipt still commits its source mirror and cursor. A
        # crash here resumes the preceding receipt and safely repeats the no-op.
        if processing and not processing['complete'] and state is self._durable_state:
            return
        await super()._save_state(state, processing)
        self._durable_state = state
        self._last_committed_at = self.now()

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
