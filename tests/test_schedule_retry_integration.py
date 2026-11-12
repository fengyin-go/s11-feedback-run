"""Observable dispatch order, authorization caps, attempts and persistent waiting clocks."""
import concurrent.futures
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from fixtures import stream_text
from test_run import DockerCase, SID
import fbrlib as lib
import fbr_run
import fbr_retry
import fbr_schedule

class FairQueue(unittest.TestCase):
 def test_failed_task_yields_to_unstarted_task(self):
  order=[];counts={}
  def call(p):
   order.append(p.name);counts[p.name]=counts.get(p.name,0)+1
   return {'task_id':p.name,'state':'retry' if p.name=='a' and counts[p.name]==1 else 'ok','retry_after':0}
  rows=fbr_schedule.rolling([Path('a'),Path('b'),Path('c')],call,parallel=1)
  self.assertEqual(order,['a','b','c','a']);self.assertEqual(len(rows),3)
 def test_waiting_retry_does_not_hold_execution_thread(self):
  now=[100.];order=[]
  def call(p):
   order.append(p.name)
   return {'task_id':p.name,'state':'retry','retry_after':110} if order==['a'] else {'task_id':p.name,'state':'ok'}
  rows=fbr_schedule.rolling([Path('a'),Path('b')],call,parallel=1,clock=lambda:now[0],sleeper=lambda seconds:now.__setitem__(0,now[0]+seconds))
  self.assertEqual(order,['a','b','a']);self.assertEqual(len(rows),2)
 def test_drain_does_not_kill_in_flight_or_dispatch_queued(self):
  drain=[False];order=[]
  def call(p):order.append(p.name);drain[0]=True;return {'task_id':p.name,'state':'ok'}
  rows=fbr_schedule.rolling([Path('a'),Path('b')],call,parallel=1,stopped=lambda:drain[0])
  self.assertEqual(order,['a']);self.assertEqual({r['task_id']:r['state'] for r in rows},{'a':'ok','b':'deferred'})

class PhysicalAttempt(DockerCase):
 def test_single_attempt_returns_retry_before_second_dispatch(self):
  task=self.make_task();self.cfg['_single_attempt']=True
  self.docker.outcomes=[{'stdout':stream_text(SID,with_result=False),'stderr':'API Error: 504 gateway\n','rc':1,'projects':self.projects('p0')},self.good()]
  row=fbr_run.run_one(task,self.cfg)
  self.assertEqual(row['state'],'retry');self.assertEqual(len(self.docker.outcomes),1)
  attempts=lib.load_json(task/'run/attempts.json');self.assertEqual(fbr_retry.ledger(attempts)['model'],0)
  self.assertEqual(fbr_run.run_one(task,self.cfg)['state'],'ok')
 def test_explicit_physical_cap_keeps_authorized_limit(self):
  task=self.make_task();self.cfg['_single_attempt']=True;self.cfg['run']['max_physical_attempts']=1
  self.docker.outcomes=[{'stdout':stream_text(SID,with_result=False),'stderr':'API Error: 504 gateway\n','rc':1,'projects':self.projects('p0')},self.good()]
  self.assertEqual(fbr_run.run_one(task,self.cfg)['state'],'retry')
  row=fbr_run.run_one(task,self.cfg)
  self.assertEqual(row['detail'],'max_physical_attempts');self.assertEqual(len(self.docker.outcomes),1)
 def test_new_attempt_uses_new_baseline_not_historical_total(self):
  task=self.make_task();lib.save_json(task/'run/budget.json',{'active_seconds':0,'waiting_seconds':2900})
  self.docker.outcomes=[self.good()]
  self.assertEqual(fbr_run.run_one(task,self.cfg)['state'],'ok')
  self.assertEqual(lib.load_json(task/'run/current.json')['attempt_waiting_base'],2900)
  self.assertGreaterEqual(lib.load_json(task/'run/budget.json')['waiting_seconds'],2900)
 def test_explicit_task_wait_authorization_is_still_hard_limit(self):
  task=self.make_task();self.cfg['run']['gateway_wait_task_total']=2700
  lib.save_json(task/'run/budget.json',{'active_seconds':0,'waiting_seconds':2900})
  self.docker.outcomes=[self.good()]
  row=fbr_run.run_one(task,self.cfg)
  self.assertEqual(row['state'],'deferred');self.assertEqual(len(self.docker.outcomes),1)
