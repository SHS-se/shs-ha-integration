"""Age limits for diagnostics only; operational ownership/accounting is separate."""
from datetime import datetime, timedelta, timezone
from shs_core.verification import VerificationJournal

DAYS = 3


class RecentVerification(VerificationJournal):
    def __init__(self, *args, now=None):
        super().__init__(*args)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.loaded = False

    async def load(self):
        await super().load()
        self.loaded = True
        await self.flush()
        await self.flush_samples()

    def retire(self):
        if not self.loaded:
            return
        cutoff = self.now() - timedelta(days=DAYS)
        def recent(row):
            return datetime.fromisoformat(row.get('last_at', row['at'])) >= cutoff
        for name in ('attempts', 'evaluations', 'events', 'samples'):
            rows = getattr(self, name)
            kept = [r for r in rows if recent(r)]
            if len(kept) == len(rows):
                continue
            setattr(self, name, kept)
            if name == 'samples':
                self.discarded_samples += len(rows) - len(kept)
                self.samples_dirty = True
            else:
                if name != 'events':
                    self.discarded += sum(r['count'] for r in rows if not recent(r))
                self.dirty = True
        records = self.attempts + self.evaluations
        scopes = {r['scope'] for r in records}
        slots = {r['slot_id'] for r in records + self.samples}
        contexts = {r['context_id'] for r in self.samples}
        self.configurations = {k:v for k,v in self.configurations.items() if k in scopes}
        self.slots = {k:v for k,v in self.slots.items() if k in slots}
        self.sample_contexts = {k:v for k,v in self.sample_contexts.items() if k in contexts}

    async def flush(self):
        self.retire()
        await super().flush()

    async def flush_samples(self):
        self.retire()
        await super().flush_samples()

    def export(self, **kwargs):
        result = super().export(**kwargs)
        result['retention']['maximum_age_days'] = DAYS
        return result
