"""验收后发布顺序与完整交付状态回归。"""
import threading
from types import SimpleNamespace
from unittest import mock
from test_sheet_batch import SheetCase
import fbr_flow
import fbr_feedback
import fbr_snapshot
import fbr_sheet
import fbrlib as lib
import fbr_recover
import fbr_reviewer

class DeliveryFields(SheetCase):
    def test_accepted_feedback_missing_required_input_is_not_green(self):
        task = self.gated()
        row, _ = self.row(task)
        self.assertTrue(row['闸门状态'].startswith('未通过：'))
        self.assertIn('缺产物快照', row['闸门状态'])
        self.assertIn('缺运行录屏', row['闸门状态'])

    def test_unknown_environment_description_is_blocked(self):
        problems = fbr_sheet.row_problems({'环境可复现等级': 'Linux arm64 已启动'}, {}, {})
        self.assertIn('环境可复现等级的取值不在选项里：Linux arm64 已启动', problems)

    def test_publish_runs_after_acceptance_and_refreshes_sheet(self):
        task = self.gated()
        events = []
        options = SimpleNamespace(batch=task.parent.name, publish=True, auto_submit=False,
                                  delivery_lock=threading.Lock())
        def gate(*args):
            events.append('acceptance')
            return ([{'task_id':task.name, 'state':'ok'}], [])
        def publish(*args):
            events.append('push')
            return {'state':'ok'}
        with mock.patch.object(fbr_feedback,'gate_task',return_value={'errors':[]}), \
             mock.patch.object(fbr_feedback,'gate_batch',side_effect=gate), \
             mock.patch.object(fbr_snapshot,'Hub'), \
             mock.patch.object(fbr_snapshot,'push_one',side_effect=publish), \
             mock.patch.object(fbr_sheet,'write_sheet',side_effect=lambda *a: events.append('sheet')), \
             mock.patch.object(fbr_sheet,'build_row',return_value=({'数据状态':'可提交'},'')), \
             mock.patch.object(fbr_sheet,'row_problems',return_value=[]):
            self.assertEqual(fbr_flow.review_task(task,self.root,self.cfg,options)['state'],'ok')
        self.assertEqual(events,['acceptance','sheet','push','sheet'])

    def test_failed_acceptance_requests_repair_without_pushing(self):
        task=self.gated()
        options=SimpleNamespace(batch=task.parent.name,publish=True,auto_submit=False,
                                delivery_lock=threading.Lock(),reviewer_env='unused')
        with mock.patch.object(fbr_feedback,'gate_task',return_value={'errors':[]}), \
             mock.patch.object(fbr_feedback,'gate_batch',return_value=([{'task_id':task.name,'state':'failed','errors':['bad']}],[])), \
             mock.patch.object(fbr_snapshot,'push_one') as push:
            self.assertEqual(fbr_flow.review_task(task,self.root,self.cfg,options)['state'],'repairing')
        push.assert_not_called()
        self.assertEqual(lib.load_json(task/'review/gate-repair.json')['attempts'][0]['errors'],['bad'])

    def test_failed_gate_requeues_then_publishes_repaired_result(self):
        task=self.gated()
        options=SimpleNamespace(batch=task.parent.name,publish=True,auto_submit=False,
                                delivery_lock=threading.Lock(),reviewer_env='unused')
        events=[]
        def gate(*args):
            failed=not events
            events.append('failed' if failed else 'ok')
            lib.set_stage(task,'feedback','failed' if failed else 'ok')
            lib.save_json(task/'review/gate.json',{'passed':not failed,'errors':['duplicate'] if failed else []})
            return ([{'task_id':task.name,'state':'failed' if failed else 'ok','errors':['duplicate'] if failed else []}],[])
        with mock.patch.object(fbr_feedback,'gate_task',return_value={'errors':[]}), \
             mock.patch.object(fbr_feedback,'gate_batch',side_effect=gate), \
             mock.patch.object(fbr_feedback,'card_one'), \
             mock.patch.object(fbr_reviewer,'credentials',return_value=('base','secret')), \
             mock.patch.object(fbr_reviewer,'trace_configuration',return_value=('trace','high')), \
             mock.patch.object(fbr_recover,'recover',return_value={'state':'already_valid'}) as repair, \
             mock.patch.object(fbr_sheet,'write_sheet'), \
             mock.patch.object(fbr_sheet,'build_row',return_value=({'数据状态':'可提交'},'')), \
             mock.patch.object(fbr_sheet,'row_problems',return_value=[]), \
             mock.patch.object(fbr_snapshot,'Hub'), \
             mock.patch.object(fbr_snapshot,'push_one',return_value={'state':'ok'}) as push:
            self.assertEqual(fbr_flow.review_task(task,self.root,self.cfg,options)['state'],'repairing')
            push.assert_not_called()
            self.assertEqual(fbr_flow.review_task(task,self.root,self.cfg,options)['state'],'ok')
        self.assertTrue(repair.call_args.kwargs['repair_gate'])
        push.assert_called_once()

    def test_default_publish_and_failure_preserve_acceptance(self):
        task=self.gated();before=(task/'review/model-review.json').read_bytes()
        options=SimpleNamespace(batch=task.parent.name,auto_submit=False,delivery_lock=threading.Lock())
        with mock.patch.object(fbr_sheet,'write_sheet'), \
             mock.patch.object(fbr_snapshot,'Hub'), \
             mock.patch.object(fbr_snapshot,'push_one',side_effect=lib.FbrError('remote main diverged')) as push:
            with self.assertRaisesRegex(lib.FbrError,'diverged'):
                fbr_flow.finish_review(task,self.root,self.cfg,options)
        push.assert_called_once()
        self.assertEqual((task/'review/model-review.json').read_bytes(),before)
        self.assertTrue(lib.stage_ok(task,'feedback'))
        self.assertEqual(lib.load_json(task/'review/delivery-repair.json')['attempts'][0]['stage'],'publish')
