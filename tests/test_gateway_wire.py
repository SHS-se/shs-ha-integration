"""Transport-only release upgrades, filtered sources and bounded projection frames."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from gateway_wire import ProjectionAssembly, filter_sources, validate_release


class WireTests(unittest.TestCase):
    def test_filtered_sensor_chain_and_cycle(self):
        rows = {key:SimpleNamespace(attributes={'entity_id':value}) for key,value in
                [('sensor.filtered','sensor.raw'),('sensor.raw','sensor.filtered')]}
        self.assertEqual(filter_sources({'sensor.filtered'},rows.get,lambda _:'filter'),set(rows))
        self.assertEqual(filter_sources({'sensor.filtered'},rows.get,lambda _:'template'),{'sensor.filtered'})

    def test_active_history_keeps_identity_but_requires_current_companion_and_core(self):
        old = dict(protocol=2,app_version='old-app',integration_version='old-companion',core_sha256='a'*64)
        current = dict(old,app_version='new-app',integration_version='new-companion')
        validate_release(current,{'pair':old},'new-companion')
        for value in (None,old,dict(current,protocol=3),dict(current,core_sha256='b'*64)):
            with self.assertRaises(ValueError):validate_release(value,{'pair':old},'new-companion')

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
