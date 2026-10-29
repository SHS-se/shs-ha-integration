"""Synthetic native precision cases, independent of household recordings."""
import unittest
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.native_numbers import canonical_metadata, equal, watts, service_watts


class NativeNumberTests(unittest.TestCase):
    def test_machine_noise_is_canonical_but_adjacent_tick_is_not_equal(self):
        a = {'min':0, 'max':100, 'step':0.001, 'unit_of_measurement':'kW'}
        self.assertEqual(watts('1.6000000000000001', a), 1600)
        self.assertEqual(service_watts(1600, a), 1.6)
        for value in range(100001):
            wire = service_watts(value, a)
            self.assertEqual(watts(wire, a), value)
            self.assertEqual(int(wire*1000), value)
            w = {**a,'unit_of_measurement':'W','max':100000,'step':1}
            self.assertEqual(service_watts(value,w),value)
            self.assertEqual(watts(value,w),value)
        from math import nextafter, inf
        self.assertEqual(canonical_metadata(nextafter(a['min'],-inf),a),a['min'])
        self.assertEqual(canonical_metadata(nextafter(a['max'],inf),a),a['max'])
        for beyond in (-a['step'],a['max']+a['step']):
            with self.assertRaises(ValueError):
                canonical_metadata(beyond,a)
        self.assertTrue(equal('1.6000000000000001', 1.6, a))
        self.assertFalse(equal('1.599', 1.6, a))
        with self.assertRaises(ValueError):
            canonical_metadata('1.6001', a)

    def test_upper_current_tick_remains_different(self):
        a = {'min':0,'max':32,'step':1}
        self.assertFalse(equal('11',10,a))
        self.assertFalse(equal('1e1000',10))
        self.assertTrue(equal('10.000000000000002',10,a))
        for value in (True, 'nan', 'inf', -1, 33):
            with self.assertRaises(ValueError):
                canonical_metadata(value, a)

class NativeCapacityLatticeTests(unittest.TestCase):
    def test_offset_lattice_uses_hardware_origin_and_preserves_real_budget(self):
        from shs_core.grid_capacity import ev_current_ceiling, capped_intention
        attributes={'min':1,'max':31,'step':2}
        self.assertEqual(ev_current_ceiling(13*690,attributes,5),13)
        self.assertEqual(ev_current_ceiling(13*690-1,attributes,5),11)
        self.assertEqual(ev_current_ceiling(3*690,attributes,5),0)
        value=dict(device='ev',operation='apply',parameters={'current_a':15,'minimum_a':5})
        capped=capped_intention(value,13*690,native=attributes,logical_minimum_a=5)
        self.assertEqual(capped['parameters']['current_a'],13)
        stopped=capped_intention(value,3*690,native=attributes,logical_minimum_a=5)
        self.assertEqual(stopped['parameters'],{'current_a':0,'minimum_a':0})

if __name__ == '__main__':
    unittest.main()
