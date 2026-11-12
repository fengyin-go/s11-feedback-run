"""平台字段与用户授权录屏修订契约。"""
from pathlib import Path
from unittest import mock
import unittest
import fixtures
import fbrlib as lib
import fbr_web
import fbr_record
import fbr_sheet
from test_record_facts import RecordCase

class WebContract(RecordCase):
    def plan(self, task):
        original = lib.load_json(task / 'recording-plan.json')
        override = {'source_sha256': lib.content_hash(original), 'authorization': '用户授权录启动和页面',
                    'plan': {'mode': 'web', 'serve_cmd': 'run {port}',
                             'steps': [{'action': 'click', 'selector': 'button'}]}}
        lib.save_json(task / 'review/recording-plan.override.json', override)
        return override

    def render_web(self, plan, container, port, out, rec):
        (out / 'raw.webm').write_bytes(b'webm')
        return [{'cmd': 'run', 'rc': None}], {'mode': 'web', 'startup_ready': True,
            'startup_seconds': 10, 'page_segment': True, 'page_seconds': 8, 'page_errors': []}

    def test_authorized_override_preserves_frozen_plan_and_binds_record(self):
        task = self.committed()
        before = (task / 'recording-plan.json').read_bytes()
        override = self.plan(task)
        with mock.patch.object(fbr_web, 'render', self.render_web):
            self.assertEqual(fbr_record.record_one(task, self.cfg)['state'], 'ok')
        meta = lib.load_json(task / 'record/recording.json')
        self.assertEqual(meta['plan_override'], override)
        self.assertEqual(meta['plan_sha256'], lib.content_hash(override['plan']))
        self.assertEqual((task / 'recording-plan.json').read_bytes(), before)

    def test_override_source_mismatch_stops_before_starting_container(self):
        task = self.committed()
        override = self.plan(task)
        override['source_sha256'] = 'wrong'
        lib.save_json(task / 'review/recording-plan.override.json', override)
        with self.assertRaisesRegex(lib.FbrError, '不匹配冻结原方案'):
            fbr_record.record_one(task, self.cfg)
        self.assertEqual(self.docker.docker_calls('run', '-d'), [])

    def test_missing_page_segment_cannot_pass(self):
        task = self.committed()
        self.plan(task)
        def render(*args):
            steps, meta = self.render_web(*args)
            meta.update(page_segment=False, page_seconds=0)
            return steps, meta
        with mock.patch.object(fbr_web, 'render', render):
            result = fbr_record.record_one(task, self.cfg)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('页面段', result['detail'])

    def test_prepared_fields_never_invents_review_or_unpublished_snapshot(self):
        task = self.committed()
        record = lib.load_json(task / 'task.json')
        record.update(os='Linux arm64', subdirection='数据采集、爬虫与RPA')
        lib.save_json(task / 'task.json', record)
        lib.save_json(task / 'review/snapshot.json', {'pushed': False, 'commit': 'a' * 40})
        fields = fbr_sheet.prepared_fields(self.root, task, self.cfg)
        self.assertEqual(fields['操作系统'], 'MacOS/Linux')
        self.assertEqual(fields['子方向'], '数据采集、爬虫与 RPA')
        self.assertEqual(fields['产物快照'], '')
        self.assertNotIn('Feedback', fields)
        self.assertNotIn('交付完整性', fields)
        self.assertEqual(fields['备注'], '')

    def test_external_health_path_rejected(self):
        with self.assertRaisesRegex(lib.FbrError, 'health_paths'):
            fbr_web.validate_plan({'serve_cmd': 'run', 'health_paths': ['//other/'],
                                  'steps': [{'action': 'click', 'selector': 'button'}]})

    def test_page_pause_accepts_bounded_milliseconds(self):
        for pause in (0, 1500, 10000):
            fbr_web.validate_plan({'serve_cmd': 'run', 'steps': [
                {'action': 'click', 'selector': 'button', 'pause_ms': pause}]})
        for pause in (-1, 10001, True, '1500', 1.5):
            with self.subTest(pause=pause), self.assertRaisesRegex(lib.FbrError, 'pause_ms'):
                fbr_web.validate_plan({'serve_cmd': 'run', 'steps': [
                    {'action': 'click', 'selector': 'button', 'pause_ms': pause}]})

if __name__ == '__main__':
    unittest.main()
