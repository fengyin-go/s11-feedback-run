"""Real stream regression: thinking token counters are not HTTP failures."""
import json
from pathlib import Path
import tempfile
import unittest

import fbr_runtime as runtime


class GatewayEventClassification(unittest.TestCase):
    def test_token_counters_do_not_start_gateway_wait(self):
        rows = [{'type': 'system', 'subtype': 'thinking_tokens',
                 'estimated_tokens': value, 'estimated_tokens_delta': 1}
                for value in (429, 500, 502, 503, 504, 529)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stream.jsonl'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            watcher = runtime.Watcher(10)
            watcher.poll(path, 20)
        self.assertEqual(watcher.events, len(rows))
        self.assertEqual(watcher.retry_at, 0)
        self.assertEqual(watcher.kind, '')

    def test_retry_metadata_is_not_an_error_status(self):
        row = {'type': 'system', 'subtype': 'api_retry', 'retry_delay_ms': 429,
               'error_status': None, 'error': 'unknown', 'uuid': '503'}
        self.assertEqual(runtime.gateway_signal(row), 'network')

    def test_live_thinking_clears_retry_without_counting_tool_use(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stream.jsonl'
            path.write_text(json.dumps({'type': 'system', 'subtype': 'api_retry',
                                        'error_status': 504}) + '\n')
            watcher = runtime.Watcher(10)
            watcher.poll(path, 20)
            self.assertGreater(watcher.retry_at, 0)
            with path.open('a') as stream:
                stream.write(json.dumps({'type': 'system', 'subtype': 'thinking_tokens',
                                         'estimated_tokens': 504, 'estimated_tokens_delta': 3}) + '\n')
            watcher.poll(path, 30)
            self.assertEqual(watcher.retry_at, 0)
            self.assertEqual(watcher.progress, 30)
            self.assertEqual(watcher.tools, 0)
            with path.open('a') as stream:
                stream.write(json.dumps({'type': 'system', 'subtype': 'api_retry',
                                         'error_status': 504}) + '\n')
            watcher.poll(path, 40)
            self.assertEqual(watcher.retry_at, 40)

    def test_invalid_or_nonpositive_thinking_does_not_clear_retry(self):
        for delta, total in ((0, 10), (-1, 10), (True, 10), ('1', 10), (3, 2), (1, None)):
            with self.subTest(delta=delta, total=total), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'stream.jsonl'
                path.write_text(json.dumps({'type': 'system', 'subtype': 'thinking_tokens',
                                            'estimated_tokens': total, 'estimated_tokens_delta': delta}) + '\n')
                watcher = runtime.Watcher(10)
                watcher.retry_at = 15
                watcher.poll(path, 20)
                self.assertEqual(watcher.retry_at, 15)
                self.assertEqual(watcher.progress, 10)

    def test_actual_api_status_and_messages_keep_their_pressure(self):
        for status, signal in ((429, 'limit'), (500, 'soft'), (504, 'soft')):
            with self.subTest(status=status):
                self.assertEqual(runtime.gateway_signal({
                    'type': 'system', 'subtype': 'api_retry',
                    'error_status': status, 'error': 'unknown'}), signal)
        for row, signal in (({'type': 'error', 'error': 'TLS connection closed'}, 'network'),
                            ({'type': 'result', 'is_error': True,
                              'result': 'API Error: rate_limit'}, 'limit')):
            self.assertEqual(runtime.gateway_signal(row), signal)

    def test_non_error_content_does_not_affect_pressure(self):
        for row in ({'type': 'user', 'message': {'content': 'HTTP 429'}},
                    {'type': 'assistant', 'message': {'content': 'HTTP 503'}},
                    {'type': 'result', 'is_error': False, 'result': '429 tests passed'},
                    {'type': 'system', 'subtype': 'init', 'uuid': '429'}):
            with self.subTest(row=row):
                self.assertEqual(runtime.gateway_signal(row), '')


if __name__ == '__main__':
    unittest.main()
