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
             mock.patch.object(fbr_sheet,'build_row',return_value=({'数据状态':'可提交'},'')):
            self.assertEqual(fbr_flow.review_task(task,self.root,self.cfg,options)['state'],'ok')
        self.assertEqual(events,['acceptance','sheet','push','sheet'])

    def test_failed_acceptance_never_pushes(self):
        task=self.gated()
        options=SimpleNamespace(batch=task.parent.name,publish=True,auto_submit=False,
                                delivery_lock=threading.Lock())
        with mock.patch.object(fbr_feedback,'gate_task',return_value={'errors':[]}), \
             mock.patch.object(fbr_feedback,'gate_batch',return_value=([{'task_id':task.name,'state':'failed','errors':['bad']}],[])), \
             mock.patch.object(fbr_snapshot,'push_one') as push:
            with self.assertRaisesRegex(lib.FbrError,'Feedback 闸门未通过'):
                fbr_flow.review_task(task,self.root,self.cfg,options)
        push.assert_not_called()
