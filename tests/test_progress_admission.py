"""Progress describes admitted work, rather than hiding real exhausted quotas."""
from unittest import mock
from fixtures import Sandbox
import fbrlib as lib
import fbr_sheet

class ProgressAdmission(Sandbox):
    def queued(self):
        task=self.make_task()
        directory=task.parent/'runner'
        lib.save_json(directory/'process.json',{'pid':123,'identity':'controller'})
        lib.save_json(directory/'plan.json',{'tasks':[task.name]})
        return task

    def live(self, info):
        return info.get('pid') == 123

    def test_live_controller_reports_admitted_queue(self):
        task=self.queued()
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertIn('等待当前批次运行槽位',fbr_sheet.run_activity(task))

    def test_exhausted_physical_cap_is_not_queued(self):
        task=self.queued()
        lib.save_json(task.parent/'runner/plan.json',{'tasks':[task.name],'run_limits':{'max_physical_attempts':1}})
        lib.save_json(task/'run/attempts.json',[{'n':1,'detail':'API Error: 504'}])
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertEqual(fbr_sheet.run_activity(task),'')

    def test_authorized_wait_cap_is_not_queued(self):
        task=self.queued()
        lib.save_json(task.parent/'runner/plan.json',{'tasks':[task.name],'run_limits':{'gateway_wait_task_total':5400}})
        lib.save_json(task/'run/budget.json',{'active_seconds':1,'waiting_seconds':5400})
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertEqual(fbr_sheet.run_activity(task),'')

    def test_drain_is_not_queued(self):
        task=self.queued()
        (task.parent/'runner/drain').touch()
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertEqual(fbr_sheet.run_activity(task),'')

    def test_expired_starting_is_no_longer_waiting_for_dispatch(self):
        task=self.queued()
        lib.save_json(task.parent/'runner/plan.json',{'tasks':[task.name],'deadline':1})
        lib.save_json(task/'run/current.json',{'state':'starting','owner_pid':123,'owner_identity':'controller'})
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertIn('停止派发', fbr_sheet.run_activity(task))

    def test_live_worker_remains_running_after_dispatch_deadline(self):
        task=self.queued()
        lib.save_json(task.parent/'runner/plan.json',{'tasks':[task.name],'deadline':1})
        lib.save_json(task/'run/current.json',{'state':'running','pid':123,'identity':'worker'})
        with mock.patch('fbr_runtime.alive',side_effect=self.live):
            self.assertIn('模型运行中', fbr_sheet.run_activity(task))
