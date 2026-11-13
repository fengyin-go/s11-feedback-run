"""Real subprocesses cross wall-clock guards while progressing, and still stop if stalled."""
import copy,json,sys,time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch
from fixtures import SCRIPTS
import fbrlib as lib
import fbr_runtime as rt
import fbr_worker

class ProgressContinuation(unittest.TestCase):
 def request(self,task,code):
  policy=copy.deepcopy(lib.DEFAULTS['run']);policy.update(stall_seconds=.16,poll_seconds=.02)
  return {'id':'progress-test','command':[sys.executable,'-u','-c',code],'stdin':'once','task':str(task),
   'stdout':str(task/'out'),'stderr':str(task/'err'),'result':str(task/'exit.json'),
   'guard_deadline':time.time()+.05,'progress_policy':policy}
 def test_worker_keeps_genuine_thinking_past_fixed_guard(self):
  with TemporaryDirectory() as d:
   p=Path(d);q=self.request(p,'import json,time\nfor i in range(12):\n print(json.dumps({"type":"system","subtype":"thinking_tokens","estimated_tokens_delta":1,"estimated_tokens":i+1}),flush=True);time.sleep(.035)\nprint(json.dumps({"type":"result","is_error":False,"result":"done"}))')
   result=fbr_worker.execute(q)
   self.assertEqual((result['rc'],result['timed_out']),(0,False))
   self.assertGreater(result['ended_epoch'],q['guard_deadline']+.25)
   self.assertEqual(len((p/'out').read_text().splitlines()),13)
 def test_worker_stops_real_silent_stall(self):
  with TemporaryDirectory() as d:
   p=Path(d);q=self.request(p,'import time;time.sleep(10)')
   with patch.object(lib,'run',return_value=Mock(returncode=1)):
    result=fbr_worker.execute(q)
   self.assertEqual((result['rc'],result['timed_out'],result['reason']),(124,True,'stalled'))
 def test_worker_preserves_gateway_wait_cap(self):
  with TemporaryDirectory() as d:
   p=Path(d);q=self.request(p,'import json,time;print(json.dumps({"type":"system","subtype":"api_retry","error_status":504}),flush=True);time.sleep(10)');q['progress_policy']['gateway_wait_limit']=.1
   with patch.object(lib,'run',return_value=Mock(returncode=1)):
    result=fbr_worker.execute(q)
   self.assertEqual(result['reason'],'gateway_wait_exhausted')
 def test_inflight_tool_survives_guard_and_quiet_period(self):
  with TemporaryDirectory() as d:
   p=Path(d);q=self.request(p,'import json,time;print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","id":"tool1"}]}}),flush=True);time.sleep(.35);print(json.dumps({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tool1"}]}}))')
   result=fbr_worker.execute(q)
   self.assertEqual((result['rc'],result['timed_out']),(0,False))
 def test_monitor_crosses_all_time_limits_with_progress(self):
  with TemporaryDirectory() as d:
   task=Path(d);(task/'run').mkdir();out=task/'out';out.write_text(json.dumps({'type':'system','subtype':'thinking_tokens','estimated_tokens_delta':1,'estimated_tokens':1})+'\n');result=task/'exit'
   cfg=copy.deepcopy(lib.DEFAULTS);cfg['_deadline']=950;cfg['run']['task_budget']=1
   req={'id':'x','result':str(result),'stdout':str(out),'started_epoch':900,'base_budget':{'active_seconds':0,'waiting_seconds':0},'timeout':1,'guard_deadline':950}
   proc=Mock(pid=999);proc.poll.return_value=None
   def finish(_):lib.save_json(result,{'id':'x','rc':0,'timed_out':False})
   with patch.object(rt.time,'time',return_value=1000),patch.object(rt.time,'sleep',side_effect=finish),patch.object(lib,'run') as docker:
    self.assertEqual(rt.monitor_process(proc,req,{'pid':999}, {'task':task,'cfg':cfg,'gateway':Mock()},started=900),(0,False))
    docker.assert_not_called()
   self.assertGreaterEqual(lib.load_json(task/'run/budget.json')['active_seconds'],100)

 def test_existing_request_can_be_recovered_after_admission_deadline(self):
  import fbr_run
  with TemporaryDirectory() as d:
   task=Path(d);(task/'run').mkdir()
   lib.save_json(task/'run/current.json',{'state':'running','request_id':'same-original-request'})
   cfg=copy.deepcopy(lib.DEFAULTS);cfg['_deadline']=time.time()-1;cfg['image_id']='sha256:'+'a'*64
   with patch.object(lib,'load_task',return_value={'task_id':task.name}),patch.object(fbr_run,'bound_key',return_value='fake'),patch.object(fbr_run,'recover_one',return_value={'state':'running','detail':'same worker'}) as recover:
    self.assertEqual(fbr_run.run_one(task,cfg)['state'],'running')
    recover.assert_called_once_with(task,cfg)

 def test_prepared_container_can_resume_after_admission_deadline(self):
  import fbr_run
  with TemporaryDirectory() as d:
   task=Path(d);(task/'run').mkdir()
   (task/'run/prompt.txt').write_text('test prompt',encoding='utf-8')
   lib.save_json(task/'run/current.json',{'state':'starting','attempt':2,'container_id':'same-container'})
   cfg=copy.deepcopy(lib.DEFAULTS);cfg['_deadline']=time.time()-1;cfg['image_id']='sha256:'+'a'*64
   record={'task_id':task.name,'prompt':'test prompt','initial_commit':'a'*40,'task_repo_full':'owner/repo','image':'test-image'}
   def timed_stream(command, *, stdin_text, stdout_path, stderr_path, timeout):
    Path(stdout_path).write_text('',encoding='utf-8')
    Path(stderr_path).write_text('',encoding='utf-8')
    return 124,True
   with patch.object(lib,'load_task',return_value=record),patch.object(fbr_run,'bound_key',return_value='fake'),patch.object(fbr_run,'recover_one',return_value={'state':'prepared','detail':'same container'}),patch.object(fbr_run,'ready_problems',return_value=[]),patch.object(fbr_run,'use_legacy_progress_recovery',return_value=True),patch.object(fbr_run,'run_command') as create:
    create.return_value=Mock(returncode=0,stdout='cid')
    with patch.object(lib,'save_git_config'),patch.object(lib,'open_permissions'),patch.object(lib,'run_stream',side_effect=timed_stream),patch.object(fbr_run,'code_changes',return_value=[]),patch.object(fbr_run,'archive_attempt'):
     row=fbr_run.run_one(task,cfg)
   self.assertIn(row['state'],('running','deferred'))

 def test_only_resumable_tasks_bypass_batch_deadline(self):
  import fbr_run
  with TemporaryDirectory() as d:
   resumable=Path(d)/'resumable';(resumable/'run').mkdir(parents=True)
   lib.save_json(resumable/'run/current.json',{'state':'starting'})
   fresh=Path(d)/'fresh';(fresh/'run').mkdir(parents=True)
   self.assertTrue(fbr_run.all_tasks_resumable([resumable]))
   self.assertFalse(fbr_run.all_tasks_resumable([resumable,fresh]))
   self.assertFalse(fbr_run.all_tasks_resumable([]))

 def test_legacy_explicit_fixed_guard_still_enforced(self):
  with TemporaryDirectory() as d:
   p=Path(d);q=self.request(p,'import time;time.sleep(10)');q['progress_policy']=None
   with patch.object(lib,'run',return_value=Mock(returncode=1)):
    result=fbr_worker.execute(q)
   self.assertEqual((result['rc'],result['reason']),(124,'worker_guard'))

 def test_worker_stop_reason_reaches_monitor_current(self):
  with TemporaryDirectory() as d:
   task=Path(d);(task/'run').mkdir();out=task/'out';out.write_text('');result=task/'exit'
   lib.save_json(result,{'id':'x','rc':124,'timed_out':True,'reason':'stalled'})
   cfg=copy.deepcopy(lib.DEFAULTS)
   req={'id':'x','result':str(result),'stdout':str(out),'started_epoch':900,'base_budget':{'active_seconds':0,'waiting_seconds':0},'timeout':1,'guard_deadline':950}
   with patch.object(rt.time,'time',return_value=1000):
    self.assertEqual(rt.monitor_process(Mock(pid=999),req,{'pid':999}, {'task':task,'cfg':cfg,'gateway':Mock()},started=900),(124,True))
   self.assertEqual(lib.load_json(task/'run/current.json')['reason'],'stalled')
