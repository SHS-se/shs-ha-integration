"""Transport-only release upgrades, filtered sources and bounded projection frames."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from gateway_wire import ProjectionAssembly, filter_sources
from shs_wire.protocol import admit, hello, offer


class WireTests(unittest.TestCase):
    def test_filtered_sensor_chain_and_cycle(self):
        rows = {key:SimpleNamespace(attributes={'entity_id':value}) for key,value in
                [('sensor.filtered','sensor.raw'),('sensor.raw','sensor.filtered')]}
        self.assertEqual(filter_sources({'sensor.filtered'},rows.get,lambda _:'filter'),set(rows))
        self.assertEqual(filter_sources({'sensor.filtered'},rows.get,lambda _:'template'),{'sensor.filtered'})

    def test_app_release_is_independent_of_companion_release(self):
        released = offer('companion-1')
        for version in ('app-1', 'app-2', 'app-3'):
            admit(hello(version), released)
        for value in (None, dict(hello('app'),protocol=2), dict(hello('app'),core_sha256='ignored')):
            with self.assertRaises(ValueError):admit(value,released)
        with self.assertRaisesRegex(ValueError,'missing: new-feature-v1'):
            admit(dict(hello('app'),requires=['new-feature-v1']),released)
        with self.assertRaisesRegex(ValueError,'unique'):
            admit(dict(hello('app'),requires=['x','x']),released)
        with self.assertRaises(ValueError):admit(hello('app'),None)

    def test_large_projection_is_atomic_ordered_and_checksummed(self):
        value = {'plan':'å'*(5*1024*1024)}
        content = json.dumps(value,separators=(',',':'))
        digest = sha256(content.encode()).hexdigest()
        receiver = ProjectionAssembly()
        for index,offset in enumerate(range(0,len(content),256*1024)):
            data = content[offset:offset+256*1024]
            last = offset+len(data)==len(content)
            frame = dict(transfer='id',index=index,last=last,data=data,sha256=digest)
            result = receiver.receive(frame)
            self.assertEqual(result,value if last else None)
        self.assertFalse(receiver.parts)
        with self.assertRaisesRegex(ValueError,'out of order'):receiver.receive(frame)
        with self.assertRaisesRegex(ValueError,'checksum'):
            receiver.receive(dict(frame,index=0,data='{}',sha256='0'*64))
        with self.assertRaisesRegex(ValueError,'object'):
            receiver.receive(dict(frame,index=0,data='null',sha256=sha256(b'null').hexdigest()))
