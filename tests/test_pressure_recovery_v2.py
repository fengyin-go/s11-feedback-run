"""Soft outages must not continually postpone a successful stable recovery."""
import copy,unittest
from fixtures import SCRIPTS
import fbrlib as l,fbr_runtime as rt
class Pressure(unittest.TestCase):
 def test_5xx_does_not_reset_stable_window_but_limit_does(self):
  options=copy.deepcopy(l.DEFAULTS['gateway'])
  state={'cap':3,'ceiling':5,'initial_drop':100,'last_drop':100,'last_limit':100,'last_ramp':100,'successes':1,'probe_ok':0}
  rt.pressure_update(state,'soft',options,401)
  self.assertEqual(state['cap'],4)
  rt.pressure_update(state,'limit',options,800)
  self.assertEqual(state['cap'],3);self.assertEqual(state['successes'],0);self.assertEqual(state['last_limit'],800)
 def test_soft_floor_dedup_and_real_limit(self):
  options=copy.deepcopy(l.DEFAULTS['gateway']);state={'cap':5,'ceiling':5}
  rt.pressure_update(state,'soft',options,100);self.assertEqual(state['cap'],4)
  rt.pressure_update(state,'soft',options,101);self.assertEqual(state['cap'],4)
  for now in (230,360,490):rt.pressure_update(state,'soft',options,now)
  self.assertEqual(state['cap'],3)
  rt.pressure_update(state,'limit',options,620);self.assertEqual(state['cap'],2)
