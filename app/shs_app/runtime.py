"""Receipt replay adapts historical accounting to a newly resumed live host."""
from shs_core.battery_runtime import BatteryRuntime


class AppBatteryRuntime(BatteryRuntime):
    async def _observe(self, group_id):
        events = await super()._observe(group_id)
        # HomeHost explicitly starts a new live-feedback epoch on resume.
        # Historical counters still pass through consume_receipt unchanged;
        # observations from before that epoch cannot authorize native control.
        if self.host and any(event.observed.observation.at_ms < self.host.state.resume_after_ms for event in events):
            return ()
        return events
