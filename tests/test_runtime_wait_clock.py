"""Runtime monitor must use attempt wait while honoring an explicit task cap."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch
from fixtures import SCRIPTS
import fbrlib as l
import fbr_runtime as rt
class WaitClock(unittest.TestCase):
 def monitor(self,total,base,task_cap=None,attempt_cap=2700):
  with TemporaryDirectory() as temp:
   task=Path(temp)/'s11-000001';(task/'run').mkdir(parents=True)
   stream=task/'run/stream.jsonl';stream.write_text(json.dumps({'type':'system','subtype':'api_retry','error_status':504})+'\n')
   result=task/'run/exit-x.json'
   budget={'active_seconds':0,'waiting_seconds':total};l.save_json(task/'run/budget.json',budget)
   cfg=copy.deepcopy(l.DEFAULTS);cfg['run'].update(gateway_wait_total=attempt_cap,gateway_wait_limit=1800,gateway_wait_task_total=task_cap);cfg['_deadline']=10000
   req={'id':'x','result':str(result),'stdout':str(stream),'started_epoch':900,'base_budget':budget,'attempt_waiting_base':base,'timeout':3600}
   cur={'attempt':2,'request_id':'x','result':str(result),'pid':999}
   proc=Mock(pid=999);proc.poll.return_value=None
   calls=[0]
   def finish(_):
    calls[0]+=1;l.save_json(result,{'id':'x','rc':0,'timed_out':False})
   gateway=Mock()
   with patch.object(rt.time,'time',return_value=1000),patch.object(rt.time,'sleep',side_effect=finish),patch.object(l,'run',return_value=Mock(returncode=0)):
    rc,timeout=rt.monitor_process(proc,req,cur,{'task':task,'cfg':cfg,'gateway':gateway},started=900)
   stop=l.load_json(task/'run/termination-x.json',default={})
   return rc,timeout,calls[0],stop,gateway
 def test_history_does_not_exhaust_new_attempt(self):
  rc,timeout,loops,stop,g=self.monitor(2900,2900)
  self.assertEqual((rc,timeout,loops),(0,False,1));self.assertEqual(stop,{})
  g.release.assert_not_called()
 def test_attempt_spans_continuation_segments(self):
  rc,timeout,loops,stop,g=self.monitor(2900,100,attempt_cap=2700)
  self.assertEqual((rc,timeout,loops),(124,True,0));self.assertEqual(stop['reason'],'gateway_wait_exhausted')
  self.assertIn('worker_exit',stop);self.assertIn('stream_bytes_before_stop',stop)
 def test_authorized_task_cap_still_applies(self):
  rc,timeout,loops,stop,g=self.monitor(5400,5400,task_cap=5400)
  self.assertTrue(timeout);self.assertEqual(stop['reason'],'gateway_wait_exhausted')
