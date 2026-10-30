"""Synthetic bounded adapter evidence: physical truth, never invented success."""
from pathlib import Path
import sys
import unittest
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.grid_capacity import (empty_capacity, prepare_capacity, dispatch_capacity,
    finish_capacity, observe_capacity, capacity_bound, commanded_controls)


class BoundedNativeSettlementTests(unittest.TestCase):
    def setUp(self):
        self.capacity = empty_capacity()
        self.effect = dict(device='battery',logical='battery',before_w=4000,after_w=1600,possible_w=4000,
            controls={'select.mode':'Command Charging (PV First)','number.charge':1.6,'number.discharge':0},
            changed='number.charge',sensor='sensor.battery',signed=True,scale=1,overhead_w=0,response_tolerance_w=100,
            latest_effect_ms=1000,native_model=dict(mode='select.mode',charge='number.charge',charge_max_w=10000,charge_unit='kW'))
        self.rows = {
            'select.mode':(11,dict(state='Command Charging (PV First)', attributes={'options':['Command Charging (PV First)']})),
            'number.charge':(12,dict(state='1.599',attributes={'min':0,'max':10,'step':0.001,'unit_of_measurement':'kW'})),
            'number.discharge':(13,dict(state='0',attributes={'min':0,'max':10,'step':0.001,'unit_of_measurement':'kW'})),
            'sensor.battery':(14,dict(state='1599',last_reported='2026-10-01T00:00:00Z',attributes={'unit_of_measurement':'W','state_class':'measurement'}))}
        prepare_capacity(self.capacity, 'lower', self.effect, 10)
        dispatch_capacity(self.capacity,'lower',10)
        finish_capacity(self.capacity,'lower','uncertain')

    def observe(self, at):
        self.capacity['receipts'] = {key:dict(ordinal=value[0],at_ms=at) for key,value in self.rows.items()}
        return observe_capacity(self.capacity,14,lambda key:self.rows.get(key,(0,None)))

    def test_real_adjacent_tick_settles_to_actual_only_after_existing_bound(self):
        self.assertEqual(self.observe(999), [])
        self.assertEqual(capacity_bound(self.capacity['devices']['battery']),4000)
        result = self.observe(1000)
        self.assertEqual(len(result),1)
        self.assertFalse(result[0][1])
        self.assertEqual(capacity_bound(self.capacity['devices']['battery']),1599)
        self.assertEqual(commanded_controls(self.capacity,'battery')['number.charge'],1.599)
        self.assertEqual(self.capacity['devices']['battery']['settled']['status'],'uncertain')

    def test_later_unresolved_write_survives_older_physical_settlement(self):
        later = {**self.effect, 'after_w':5000,'possible_w':5000,'latest_effect_ms':2000}
        prepare_capacity(self.capacity,'later',later,15)
        dispatch_capacity(self.capacity,'later',15)
        finish_capacity(self.capacity,'later','uncertain')
        self.observe(1000)
        self.assertEqual(set(self.capacity['devices']['battery']['effects']),{'later'})
        self.assertEqual(capacity_bound(self.capacity['devices']['battery']),5000)

    def test_generic_uncertain_effect_has_no_invented_deadline(self):
        del self.capacity['devices']['battery']['effects']['lower']['latest_effect_ms']
        self.assertEqual(self.observe(10**9),[])
        self.assertEqual(capacity_bound(self.capacity['devices']['battery']),4000)

if __name__ == '__main__':
    unittest.main()
