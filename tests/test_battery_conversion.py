import sys
from pathlib import Path
import unittest
sys.path.append(str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))
from shs_core.battery_conversion import Curve, Conversion, LossWindow, planning_physics, fit_branch, conversion_model, windows_from_statistics

class ConversionTests(unittest.TestCase):
    def test_tiny_commands_cannot_erase_installation_overhead(self):
        m=Conversion('diagnostics18',Curve(.95,0),Curve(.95,0),Curve(.9885020822325284,162.58072673773927),125.82819513666756)
        idle=m.net_grid(0,0,0,808)
        for watts in (1,65,73,150,1000):
            self.assertAlmostEqual(m.net_grid(watts,0,0,808)-idle,watts/.95)
            self.assertLessEqual(idle-m.net_grid(0,watts,0,808),watts)
        self.assertAlmostEqual(m.net_grid(0,65,0,808),808+162.58072673773927-.9885020822325284*65)

    def test_fits_fixed_overhead_separately_from_gain(self):
        rows=[LossWindow(i*300000,(i+1)*300000,'discharge',x/12,(.987*x-161)/12,'sources',(.013*x+161)/12) for i,x in enumerate(range(600,3600,300))]
        fit=fit_branch(rows,'discharge')
        self.assertEqual(fit['state'],'measured')
        self.assertAlmostEqual(fit['curve']['gain'],.987)
        self.assertAlmostEqual(fit['curve']['overhead_w'],161)
        model,evidence=conversion_model(rows,charge_efficiency=.95,discharge_efficiency=.95,source_revision='sources')
        self.assertEqual(evidence['discharge']['model_source'],'measured')
        self.assertEqual(evidence['grid_charge']['model_source'],'configured')
        self.assertAlmostEqual(model.net_grid(0,model.discharge.input(1000),0,1000),0)
        self.assertNotAlmostEqual(model.discharge.output(1000)/1000,model.discharge.output(3000)/3000)
    def test_narrow_power_range_cannot_identify_gain_and_overhead(self):
        rows=[LossWindow(i*300000,(i+1)*300000,'grid_charge',9200/12,8700/12,'s',500/12) for i in range(20)]
        self.assertEqual(fit_branch(rows,'grid_charge')['state'],'insufficient_power_variation')
    def test_solar_house_subtraction_does_not_claim_pure_dc_efficiency(self):
        rows=[LossWindow(i*300000,(i+1)*300000,'surplus_charge',x/12,x/12,'s',0) for i,x in enumerate(range(600,3600,300))]
        self.assertEqual(fit_branch(rows,'surplus_charge')['state'],'source_paths_not_isolated')
    def test_statistics_subtracts_grid_import_and_preserves_signed_residual(self):
        series={key:[dict(start=0,mean=value,min=value,max=value)] for key,value in dict(battery=-2,house=2.7,pv=0,grid=.9).items()}
        rows=windows_from_statistics(series,source_revision='s',end_ms=300000)
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0].output_wh,1800/12)
        self.assertAlmostEqual(rows[0].raw_loss_wh,200/12)
        series['house'][0]['max']=3.2
        unsteady=windows_from_statistics(series,source_revision='s',end_ms=300000)
        self.assertEqual([(w.branch,w.stable) for w in unsteady],[('discharge',False)])
        self.assertAlmostEqual(unsteady[0].output_wh,1800/12)
        series['battery'][0]['max']=.5
        self.assertEqual(windows_from_statistics(series,source_revision='s',end_ms=300000),())
    def test_complete_aligned_windows_only_and_configuration_separates_evidence(self):
        series={key:[dict(start=0,mean=v,min=v,max=v)] for key,v in dict(battery=1,house=.5,pv=0,grid=1.7).items()}
        self.assertEqual(windows_from_statistics(series,source_revision='s',end_ms=299999),())
        rows=windows_from_statistics(series,source_revision='s',end_ms=300000)
        model,fits=conversion_model(rows,charge_efficiency=.9,discharge_efficiency=.9,source_revision='new')
        self.assertEqual(fits['grid_charge']['windows'],0)
        self.assertEqual(Conversion.read(model.wire()),model)
    def test_charge_sources_and_overhead_count_once(self):
        m=Conversion('s',Curve(.95,100),Curve(.98,20),Curve(.99,160),30)
        self.assertAlmostEqual(m.net_grid(0,0,0,1000),1030)
        self.assertAlmostEqual(m.net_grid(950,0,0,1000),1000+1050/.95)
        solar,grid=m.charge_inputs(3000,3000,1000)
        self.assertAlmostEqual(solar,2000)
        self.assertAlmostEqual(grid,(3000-1940+100)/.95)
    def test_constant_power_charging_is_measured_by_its_energy_ratio(self):
        # User requirement, 26 October 2026: efficiency is measured, not assumed.
        rows=[LossWindow(i*300000,(i+1)*300000,'grid_charge',8830/12,8352/12,'s',478/12) for i in range(12)]
        model,fits=conversion_model(rows,charge_efficiency=.95,discharge_efficiency=.95,source_revision='s')
        self.assertEqual(fits['grid_charge']['state'],'insufficient_power_variation')
        self.assertEqual(fits['grid_charge']['model_source'],'measured_energy_ratio')
        self.assertAlmostEqual(model.grid_charge.gain,8352/8830)
        self.assertEqual(model.grid_charge.overhead_w,0)
        self.assertEqual(fits['surplus_charge']['model_source'],'configured')
        few,fits=conversion_model(rows[:5],charge_efficiency=.95,discharge_efficiency=.95,source_revision='s')
        self.assertEqual(fits['grid_charge']['model_source'],'configured')
        self.assertEqual(few.grid_charge.gain,.95)

    def test_unsteady_windows_measure_energy_but_not_the_power_curve(self):
        steady=[LossWindow(i*300000,(i+1)*300000,'discharge',x/12,(.976*x-139)/12,'s',(.024*x+139)/12) for i,x in enumerate(range(600,3600,300))]
        unsteady=[LossWindow((20+i)*300000,(21+i)*300000,'discharge',1200/12,900/12,'s',300/12,False) for i in range(10)]
        alone=fit_branch(steady,'discharge')
        fit=fit_branch(steady+unsteady,'discharge')
        self.assertEqual((fit['state'],fit['windows'],fit['stable_windows']),('measured',20,10))
        self.assertEqual(fit['curve'],alone['curve'])
        energy=sum(w.input_wh for w in steady+unsteady)
        self.assertAlmostEqual(fit['effective_energy_ratio'],sum(w.output_wh for w in steady+unsteady)/energy)
        only=fit_branch(unsteady,'discharge')
        self.assertEqual(only['state'],'insufficient_power_variation')
        self.assertAlmostEqual(only['effective_energy_ratio'],.75)

    def test_frequent_idle_windows_cannot_crowd_out_a_rare_branch(self):
        rows=[LossWindow(i*300000,(i+1)*300000,'discharge',1200/12,1080/12,'s',120/12) for i in range(6)]
        rows+=[LossWindow((10+i)*300000,(11+i)*300000,'idle',120/12,0,'s',120/12) for i in range(2500)]
        model,fits=conversion_model(rows,charge_efficiency=.95,discharge_efficiency=.95,source_revision='s')
        self.assertEqual(fits['discharge']['model_source'],'measured_energy_ratio')
        self.assertAlmostEqual(model.discharge.gain,.9)
        self.assertEqual(fits['idle']['windows'],2000)

    def test_planning_receives_conversion_and_storage_as_separate_boundaries(self):
        # User requirement, 28 October: live planning and simulation use the
        # refined installation curves rather than aggregate AC percentages.
        steady=[LossWindow(i*300000,(i+1)*300000,'discharge',x/12,(.976*x-139)/12,'s',(.024*x+139)/12) for i,x in enumerate(range(600,3600,300))]
        model,fits=conversion_model(steady,charge_efficiency=.95,discharge_efficiency=.95,source_revision='s')
        self.assertEqual(fits['discharge']['model_source'],'measured')
        storage={'charge':{'state':'measured','ratio':.988},'discharge':{'state':'measured','ratio':.968}}
        result=planning_physics(model,storage)
        self.assertEqual(result['discharge_efficiency'],.968)
        self.assertEqual(result['charge_efficiency'],.988)
        self.assertEqual(result['conversion'],model.wire())
        unmeasured=planning_physics(model,{})
        self.assertEqual(unmeasured['charge_efficiency'],1)
        self.assertEqual(unmeasured['discharge_efficiency'],1)
        self.assertEqual(unmeasured['conversion'],model.wire())
