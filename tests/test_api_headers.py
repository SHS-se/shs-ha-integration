"""Every request names the core version, and the app version when an app runs it."""
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import ssl
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.api import ShsApiClient, ShsApiError  # noqa: E402
from shs_core.api_contract import INTEGRATION_VERSION  # noqa: E402


class Session:
    def __init__(self):
        self.headers = []
        self.options = []

    @asynccontextmanager
    async def request(self, method, url, *, headers, **_options):
        self.headers.append(headers)
        self.options.append(_options)
        yield SimpleNamespace(status=200, headers={}, read=AsyncMock(return_value=b'{}'),
                              json=AsyncMock(return_value={}))


async def sent_headers(**client_options):
    """Headers of one request; the empty reply's own failure is not the point."""
    session = Session()
    try:
        await ShsApiClient(session, 'https://example.invalid', 'token', **client_options)._request('GET', 'integration-status')
    except ShsApiError:
        pass
    return session.headers[0]


class VersionHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_requests_use_the_patched_trust_store(self):
        session = Session()
        with self.assertRaises(ShsApiError):
            await ShsApiClient(session, 'https://example.invalid', 'token')._request('GET', 'integration-status')
        context = session.options[0]['ssl']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        certificates = context.get_ca_certs()
        self.assertGreater(len(certificates), 0)
        names = {value for certificate in certificates for rdn in certificate['subject']
                 for key, value in rdn if key == 'commonName'}
        self.assertFalse(names & {'AAA Certificate Services', 'emSign ECC Root CA - G3', 'emSign Root TLS CA - G3'})

    async def test_app_version_is_sent_beside_the_core_version(self):
        headers = await sent_headers(app_version='0.1.0-beta.102')
        self.assertEqual(headers['X-SHS-Integration-Version'], INTEGRATION_VERSION)
        self.assertEqual(headers['X-SHS-App-Version'], '0.1.0-beta.102')

    async def test_a_core_without_an_app_sends_only_its_own_version(self):
        self.assertNotIn('X-SHS-App-Version', await sent_headers())


if __name__ == '__main__':
    unittest.main()
