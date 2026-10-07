"""Independent backend credentials and durable, explicit control-seat selection."""
import asyncio
from copy import deepcopy
from shs_core.const import CONF_BASE_URL, CONF_DEVICE_TOKEN, DEFAULT_BASE_URL
from shs_core.gateway_journal import GatewayConflict, digest
from .records import RecordStore

URLS = {'production': DEFAULT_BASE_URL,
        'test': 'https://vxqpgbzseckgceopitpm.supabase.co/functions/v1'}


class BackendSetupError(ValueError):
    def __init__(self, message, field_errors):
        super().__init__(message)
        self.field_errors = field_errors


class BackendChanged(Exception):
    """Restart the sole controller using a durably selected backend."""


class Backends:
    def __init__(self, root, identity):
        self.root, self.identity = root, identity
        self.record = RecordStore(root/'backends.json')
        self.lock = asyncio.Lock()
        self.data = None
        self.errors = {}

    async def load(self, credentials):
        async with self.lock:
            self.data = await self.record.async_load()
            if self.data is None:
                if (self.root/'backend-adoption.json').exists():
                    raise GatewayConflict('Backend settings are missing after adoption; restore the saved record')
                url = credentials[CONF_BASE_URL].rstrip('/')
                source = next((key for key, value in URLS.items() if value == url), None)
                if source is None:
                    raise GatewayConflict('Select a production or test endpoint before upgrading backend settings')
                # Copy through the record envelope before committing adoption. A
                # crash before the marker can repeat exactly this one-time copy.
                entry = self.identity['entry_id']
                old = await RecordStore(self.root/'stores'/f'shs_energy.{entry}').async_load()
                if old is not None:
                    await RecordStore(self.root/'stores'/f'shs_energy.cloud.{source}.{entry}').async_save(old)
                self.data = dict(schema=1, identity=self.identity, revision=1,
                    controlling=source, admitted_for=source,
                    credentials={source: deepcopy(credentials)}, requests={})
                await self.record.async_save(self.data)
                await RecordStore(self.root/'backend-adoption.json').async_save({'adopted':True})
            elif self.data.get('schema') != 1 or self.data.get('identity') != self.identity:
                raise GatewayConflict('Unsupported backend settings or installation identity')
            if not (self.root/'backend-adoption.json').exists():
                await RecordStore(self.root/'backend-adoption.json').async_save({'adopted':True})
            if self.controlling not in self.data['credentials']:
                raise GatewayConflict('Selected backend has no pairing credential')

    @property
    def controlling(self):
        return self.data['controlling']

    def credentials(self, environment=None):
        return deepcopy(self.data['credentials'][environment or self.controlling])

    def public_status(self, households):
        rows = []
        for environment in URLS:
            h = households.get(environment)
            plan = h.optimisation_plan if h else None
            rows.append(dict(environment=environment, paired=environment in self.data['credentials'],
                selected=environment==self.controlling, last_delivery=h.last_optimisation_push if h else None,
                plan_id=plan.get('plan_id') if plan else None,
                subscription_active=h.data.get('subscription_active') if h and h.data else None,
                website_url=('https://smarthomesolutions.se' if environment=='production' else 'https://test.smarthomesolutions.se')+'/portal/account',
                error=self.errors.get(environment) or (h.last_optimisation_error if h else None)))
        return dict(revision=self.data['revision'], selected=self.controlling, environments=rows)

    async def change(self, environment, expected_revision, request_id, *, credentials=None):
        if environment not in URLS or type(expected_revision) is not int or not isinstance(request_id,str) or not request_id:
            raise ValueError('A known backend, revision and request identity are required')
        async with self.lock:
            request_digest = digest(dict(environment=environment, revision=expected_revision, credentials=credentials))
            prior = self.data['requests'].get(request_id)
            if prior:
                if prior['digest'] != request_digest:
                    raise GatewayConflict('Backend request identity reused with different values')
                return
            if expected_revision != self.data['revision']:
                raise GatewayConflict('Backend settings changed; refresh before saving')
            if credentials is None and environment not in self.data['credentials']:
                raise ValueError('Pair this backend before selecting its plan')
            updated = deepcopy(self.data)
            if credentials is not None:
                if not credentials.get(CONF_DEVICE_TOKEN) or credentials.get(CONF_BASE_URL) != URLS[environment]:
                    raise ValueError('Pairing returned invalid backend credentials')
                updated['credentials'][environment] = deepcopy(credentials)
            else:
                updated['controlling'] = environment
            updated['revision'] += 1
            updated['requests'] = dict(list(updated['requests'].items())[-127:])
            updated['requests'][request_id] = dict(digest=request_digest)
            await self.record.async_save(updated)
            self.data = updated

    async def settled(self):
        async with self.lock:
            updated = dict(self.data, admitted_for=self.controlling)
            await self.record.async_save(updated)
            self.data = updated
