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
            'startup_seconds': 10, 'page_segment': True, 'page_ready': True,
            'page_seconds': 12, 'page_errors': [], 'action_errors': [],
            'coverage_declared': False, 'coverage_complete': True,
            'missing_pages': [], 'execution_complete': True, 'pages': []}

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

    def test_new_batch_web_plan_requires_declared_page_coverage(self):
        task = self.committed()
        record = lib.load_json(task / 'task.json')
        record['producer'] = 's11-gsb-prompt-create'
        lib.save_json(task / 'task.json', record)
        self.plan(task)

        with mock.patch.object(fbr_web, 'render') as render:
            with self.assertRaisesRegex(lib.FbrError, '必须声明 pages'):
                fbr_record.record_one(task, self.cfg)

        render.assert_not_called()

    def test_declared_pages_require_each_page_to_have_an_action(self):
        with self.assertRaisesRegex(lib.FbrError, '页面缺少实际操作或结果步骤'):
            fbr_web.validate_plan({
                'serve_cmd': 'run',
                'pages': [
                    {'id': 'home', 'path': '/'},
                    {'id': 'details', 'path': '/details'},
                ],
                'steps': [
                    {'action': 'click', 'selector': 'button', 'page': 'home'},
                ],
            }, require_pages=True)

    def test_recording_plan_precheck_uses_same_page_contract(self):
        task = self.committed()
        record = lib.load_task(task)
        record["producer"] = "s11-gsb-prompt-create"
        with self.assertRaisesRegex(lib.FbrError, '必须声明 pages'):
            fbr_record.validate_recording_plan(record, {
                'mode': 'web',
                'serve_cmd': 'run {port}',
                'steps': [{'action': 'click', 'selector': 'button'}],
            }, self.cfg)
        ready = fbr_record.validate_recording_plan(record, {
            'mode': 'web',
            'serve_cmd': 'run {port}',
            'pages': [{'id': 'home', 'path': '/'}],
            'steps': [{'action': 'click', 'selector': 'button', 'page': 'home'}],
        }, self.cfg)
        self.assertEqual(ready['mode'], 'web')

    def test_declared_page_coverage_must_complete(self):
        task = self.committed()
        self.plan(task)
        def render(*args):
            steps, meta = self.render_web(*args)
            meta.update(coverage_declared=True, coverage_complete=False,
                        missing_pages=['details'])
            return steps, meta
        with mock.patch.object(fbr_web, 'render', render):
            result = fbr_record.record_one(task, self.cfg)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('页面覆盖不完整', result['detail'])

    def test_complete_page_coverage_keeps_product_action_errors_as_evidence(self):
        task = self.committed()
        self.plan(task)
        def render(*args):
            steps, meta = self.render_web(*args)
            meta.update(action_errors=['业务按钮返回 500'], status='page_error')
            return steps, meta
        with mock.patch.object(fbr_web, 'render', render):
            result = fbr_record.record_one(task, self.cfg)
        self.assertEqual(result['state'], 'ok')
        metadata = lib.load_json(task / 'record/recording.json')
        self.assertEqual(metadata['action_errors'], ['业务按钮返回 500'])
        self.assertEqual(metadata['status'], 'page_error')

    def test_page_screenshots_are_preserved_outside_attempt_cache(self):
        task = self.committed()
        self.plan(task)
        def render(plan, container, port, out, rec):
            (out / 'raw.webm').write_bytes(b'webm')
            (out / 'pages').mkdir()
            (out / 'pages/home.png').write_bytes(b'png')
            steps, meta = self.render_web(plan, container, port, out, rec)
            meta.update(pages=[{'id': 'home', 'screenshot': 'home.png'}],
                        page_screenshots=['home.png'])
            return steps, meta
        with mock.patch.object(fbr_web, 'render', render):
            self.assertEqual(fbr_record.record_one(task, self.cfg)['state'], 'ok')
        self.assertEqual((task / 'record/pages/home.png').read_bytes(), b'png')
        metadata = lib.load_json(task / 'record/recording.json')
        self.assertEqual(metadata['pages'][0]['screenshot'], 'record/pages/home.png')
        self.assertEqual(metadata['page_screenshots'], ['record/pages/home.png'])

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
