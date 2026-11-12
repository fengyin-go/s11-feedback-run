import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fbr_submit as submit
import fbrlib as lib

class StorageKeyTest(unittest.TestCase):
    def test_real_upload_response_keeps_key_unchanged(self):
        info={'name':'session.jsonl','path':'uploads/49fc066e3d1d456398d10a57f4e134f3.jsonl','size':1989840}
        self.assertEqual(submit.validate_uploaded('trace_file',info,{'bytes':1989840}),info)

    def test_key_rejects_other_namespaces_traversal_and_urls(self):
        for value in ('other/file.jsonl','uploads/../secret','uploads/./file','uploads//file',
                      'uploads/%2e%2e/file','uploads/file?x=1','uploads/file#part',
                      'http://bad.test/file','//bad.test/file','uploads/\\file'):
            with self.subTest(value=value),self.assertRaises(lib.FbrError):
                submit.media_url(value)

class LiveResponseContractTest(unittest.TestCase):
    def test_flat_response_requires_all_fields(self):
        fields = {key:'value' for key in (*submit.FIELD_MAP,*submit.MEDIA_TYPES)}
        self.assertEqual(submit.remote_data({'id':549,**fields}),fields)
        with self.assertRaises(lib.FbrError):submit.remote_data({'id':549})

    def test_trace_replacement_requires_identical_remote_bytes(self):
        import hashlib,io
        from unittest.mock import patch,Mock
        raw=b'actual trace\n'
        expected={'trace_file':[{'name':'s.jsonl','path':'uploads/abc.jsonl','size':len(raw)}]}
        actual={'trace_file':[{'name':'s.jsonl','url':'https://media.example/trace','size':len(raw)}]}
        local={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
        response=io.BytesIO(raw);response.geturl=lambda:'https://media.example/trace'
        with patch.object(submit.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response))):
            result=submit.verify_remote_trace(actual,expected,local)
        self.assertEqual(result['sha256'],local['sha256'])
        response=io.BytesIO(b'wrong trace!\n');response.geturl=lambda:'https://media.example/trace'
        with patch.object(submit.urllib.request,'build_opener',return_value=Mock(open=Mock(return_value=response))), self.assertRaises(lib.FbrError):
            submit.verify_remote_trace(actual,expected,local)

    def test_tls_handshake_is_distinct_from_unknown_failure(self):
        import ssl,urllib.error
        import fbr_platform as transport
        handshake={'__name__':'ssl','error':ssl.SSLEOFError(8,'eof')}
        exec('def do_handshake():\n    raise error',handshake)
        connection={'__name__':'http.client','handshake':handshake['do_handshake']}
        exec('def connect():\n    handshake()',connection)
        try:connection['connect']()
        except ssl.SSLError as exc:
            self.assertTrue(transport.tls_before_http(urllib.error.URLError(exc)))
        self.assertFalse(transport.tls_before_http(TimeoutError('read timed out')))
        self.assertFalse(transport.tls_before_http(ssl.SSLEOFError(8,'no handshake evidence')))
