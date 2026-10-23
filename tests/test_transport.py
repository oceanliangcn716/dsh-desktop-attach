import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_attach',ROOT/'attach.py')
attach=importlib.util.module_from_spec(spec)
spec.loader.exec_module(attach)

class Writer:
    def __init__(self): self.data=bytearray()
    def write(self,value): self.data.extend(value)
    async def drain(self): pass

class TransportTests(unittest.TestCase):
    def test_parse_and_mobile_patch_idempotence(self):
        parsed=attach.parse_request_head(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertEqual(parsed[0],'GET')
        html=b'<html><head></head><body></body></html>'
        patched=attach.apply_mobile_patch(html)
        self.assertEqual(attach.apply_mobile_patch(patched),patched)
        self.assertIn(attach.MOBILE_PATCH_MARKER,patched)

    def test_cookie_requires_exact_name_and_value(self):
        self.assertTrue(attach.cookie_has('other=1; sample=example','sample','example'))
        self.assertFalse(attach.cookie_has('sample=incorrect','sample','example'))
        self.assertFalse(attach.cookie_has('other=example','sample','example'))

class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpaired_private_request_never_discovers_host_or_reads_credentials(self):
        writer=Writer()
        with patch.object(attach,'discover_desktop_port') as discover,patch.object(attach,'load_signing_secret') as credential:
            await attach.handle_request(b'GET /private-check HTTP/1.1\r\nHost: localhost\r\n\r\n',None,writer,'example')
            self.assertIn(b'401 Unauthorized',writer.data)
            discover.assert_not_called()
            credential.assert_not_called()

if __name__=='__main__': unittest.main()
