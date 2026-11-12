"""平台 transport/auth：仅使用临时假凭据和回环 HTTP，不读真实账号、不访问外网。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import fixtures  # 将 scripts 加入模块路径；不会创建夹具或读取真实凭据
import fbrlib as lib
import fbr_platform as platform


USERNAME = "gonghao"
PASSWORD = "offline-test-password-123456"
FINGERPRINT = "a" * 64


class LocalAPI:
    """可审查请求的本地假平台。所有路由默认返回 JSON 对象。"""

    def __init__(self):
        self.requests = []
        self.routes = {}
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                request = {"method": self.command, "path": self.path,
                           "headers": dict(self.headers), "body": body}
                api.requests.append(request)
                route = api.routes.get((self.command, urlsplit(self.path).path))
                if callable(route):
                    route = route(request)
                if route is None:
                    route = (200, {"ok": True}, [])
                status, payload, headers = route
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.reply("POST", "/api/v1/auth/login", {"user": {"username": USERNAME}}, headers=[
            ("Set-Cookie", "solo_qa_session=offline-session; Path=/; HttpOnly"),
            ("Set-Cookie", "solo_qa_csrf=csrf%2Foffline%2Btoken; Path=/"),
        ])

    def reply(self, method, path, payload, *, status=200, headers=()):
        self.routes[(method, path)] = (status, payload, list(headers))

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class CredentialCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fbr-platform-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"HOME": str(self.home)}).start()
        self.directory = self.home / ".codex/coding-agent-satisfaction"
        self.account_path = self.directory / "solo-qa-accounts/gonghao.json"
        self.fallback_path = self.directory / "solo-qa.json"

    def credential(self, *, path=None, mode=0o600, **changes):
        path = path or self.account_path
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"username": USERNAME, "password": PASSWORD}
        doc.update(changes)
        if path.exists():
            path.chmod(0o600)
        path.write_text(json.dumps(doc), encoding="utf-8")
        path.chmod(mode)
        return path

    def assert_private_error(self, function):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with self.assertRaises(platform.PlatformError) as caught:
                function()
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertNotIn(PASSWORD, out.getvalue())
        return caught.exception


class Credentials(CredentialCase):
    def test_defaults_are_fixed_and_errors_use_fbr_error(self):
        self.assertEqual(platform.BASE_URL, "https://solo2.jzxhnh.com")
        self.assertEqual(platform.API_PREFIX, "/api/v1")
        self.assertTrue(issubclass(platform.PlatformError, lib.FbrError))

    def test_default_account_file_is_read_from_temporary_home(self):
        self.credential()
        credentials = platform.load_credentials()
        self.assertEqual(credentials["username"], USERNAME)
        self.assertEqual(credentials["password"], PASSWORD)

    def test_account_file_precedes_legacy_fallback(self):
        self.credential()
        self.credential(path=self.fallback_path, password="unused-fallback-password")
        self.assertEqual(platform.load_credentials()["password"], PASSWORD)

    def test_primary_username_must_match_account_without_using_fallback(self):
        self.credential(username="someone-else")
        self.credential(path=self.fallback_path)
        self.assert_private_error(platform.load_credentials)

    def test_matching_legacy_fallback_is_supported(self):
        self.credential(path=self.fallback_path)
        self.assertEqual(platform.load_credentials()["username"], USERNAME)

    def test_legacy_fallback_must_match_selected_account(self):
        self.credential(path=self.fallback_path, username="someone-else")
        self.assert_private_error(platform.load_credentials)

    def test_missing_credentials_raise_without_printing_secrets(self):
        self.assert_private_error(platform.load_credentials)

    def test_mode_must_be_exactly_600_without_fallback_on_invalid_primary(self):
        self.credential(path=self.fallback_path)
        for mode in (0o644, 0o640, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.credential(mode=mode)
                self.assert_private_error(platform.load_credentials)

    def test_account_symlink_is_rejected(self):
        outside = self.credential(path=self.home / "outside.json")
        self.account_path.parent.mkdir(parents=True)
        self.account_path.symlink_to(outside)
        self.assert_private_error(platform.load_credentials)

    def test_fallback_symlink_is_rejected(self):
        outside = self.credential(path=self.home / "outside.json")
        self.fallback_path.parent.mkdir(parents=True)
        self.fallback_path.symlink_to(outside)
        self.assert_private_error(platform.load_credentials)

    def test_account_directory_is_rejected(self):
        self.account_path.mkdir(parents=True)
        self.assert_private_error(platform.load_credentials)

    def test_malformed_credentials_do_not_echo_file_contents(self):
        path = self.credential()
        path.write_text('{"password": "' + PASSWORD + '", broken', encoding="utf-8")
        self.assert_private_error(platform.load_credentials)

    def test_username_and_password_must_be_nonempty_strings(self):
        for change in ({"username": ""}, {"password": ""}, {"password": None}, {"username": 1}):
            with self.subTest(change=change):
                self.credential(**change)
                self.assert_private_error(platform.load_credentials)

    def test_saved_base_url_is_ignored_and_constructor_cannot_override_it(self):
        self.credential(base_url="https://credential-controlled.invalid")
        self.assertNotIn("base_url", platform.load_credentials())
        with self.assertRaises(TypeError):
            platform.Client(base_url="https://credential-controlled.invalid")


class HTTPCase(CredentialCase):
    def setUp(self):
        super().setUp()
        self.credential(base_url="https://credential-controlled.invalid")
        self.api = LocalAPI()
        self.addCleanup(self.api.close)
        patch.object(platform, "BASE_URL", self.api.base_url).start()
        self.client = platform.Client(timeout=2)

    def calls(self, method=None, path=None):
        return [r for r in self.api.requests
                if (method is None or r["method"] == method)
                and (path is None or urlsplit(r["path"]).path == path)]


class Transport(HTTPCase):
    def test_request_uses_fixed_api_prefix_and_does_not_implicitly_login(self):
        self.api.reply("GET", "/api/v1/probe", {"raw": {"value": 3}})
        self.assertEqual(self.client.request("GET", "/probe"), {"raw": {"value": 3}})
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["path"], "/api/v1/probe")

    def test_request_preserves_query_values_and_json_body(self):
        payload = {"中文": "正文", "nested": {"n": 3}}
        result = self.client.request("POST", "/probe", json=payload,
                                     params={"q": "中文 空格", "page": 2})
        self.assertEqual(result, {"ok": True})
        request, = self.calls()
        self.assertEqual(parse_qs(urlsplit(request["path"]).query),
                         {"q": ["中文 空格"], "page": ["2"]})
        self.assertEqual(json.loads(request["body"]), payload)
        self.assertIn("application/json", request["headers"].get("Content-Type", ""))

    def test_login_only_posts_once_and_returns_raw_response(self):
        expected = {"user": {"username": USERNAME}}
        self.assertEqual(self.client.login(), expected)
        self.assertEqual(self.client.login(), expected)
        login, = self.calls("POST", "/api/v1/auth/login")
        self.assertEqual(json.loads(login["body"]), {"username": USERNAME, "password": PASSWORD})

    def test_cookie_and_decoded_csrf_are_kept_in_memory(self):
        self.client.login()
        self.client.request("POST", "/probe", json={"hello": "world"})
        self.client.request("GET", "/probe")
        unsafe, safe = self.calls(path="/api/v1/probe")
        self.assertIn("solo_qa_session=offline-session", unsafe["headers"].get("Cookie", ""))
        lowered = {k.lower(): v for k, v in unsafe["headers"].items()}
        self.assertEqual(lowered.get("x-csrf-token"), "csrf/offline+token")
        self.assertNotIn("x-csrf-token", {k.lower(): v for k, v in safe["headers"].items()})
        self.assertEqual(set(self.directory.rglob("*")),
                         {self.account_path.parent, self.account_path})

    def test_public_methods_lazy_login_and_use_documented_routes(self):
        responses = {
            ("GET", "/api/v1/auth/me"): {"username": USERNAME},
            ("GET", "/api/v1/submissions/form-schema"): {"schema": {"fields": []}, "fingerprint": "fp"},
            ("GET", "/api/v1/submissions"): {"items": [{"id": 7}], "total": 1},
            ("GET", "/api/v1/submissions/7"): {"id": 7, "data": {}},
            ("POST", "/api/v1/submissions"): {"id": 8, "data": {"title": "示例"}},
            ("PUT", "/api/v1/submissions/7"): {"id": 7, "data": {"title": "修正"}},
        }
        for (method, path), response in responses.items():
            self.api.reply(method, path, response)
        self.assertEqual(self.client.me(), responses[("GET", "/api/v1/auth/me")])
        self.assertEqual(self.client.schema(), responses[("GET", "/api/v1/submissions/form-schema")])
        self.assertEqual(self.client.list_submissions(page=2, page_size=10, status="draft"),
                         responses[("GET", "/api/v1/submissions")])
        self.assertEqual(self.client.detail(7), responses[("GET", "/api/v1/submissions/7")])
        self.assertEqual(self.client.create({"title": "示例"}, FINGERPRINT),
                         responses[("POST", "/api/v1/submissions")])
        self.assertEqual(self.client.update(7, {"title": "修正"}, FINGERPRINT, "按打回原因修改"),
                         responses[("PUT", "/api/v1/submissions/7")])
        self.assertEqual(len(self.calls("POST", "/api/v1/auth/login")), 1)
        listing, = self.calls("GET", "/api/v1/submissions")
        self.assertEqual(parse_qs(urlsplit(listing["path"]).query),
                         {"page": ["2"], "page_size": ["10"], "status": ["draft"]})
        creation, = self.calls("POST", "/api/v1/submissions")
        self.assertEqual(json.loads(creation["body"]),
                         {"data": {"title": "示例"}, "schema_fingerprint": FINGERPRINT})
        update, = self.calls("PUT", "/api/v1/submissions/7")
        self.assertEqual(json.loads(update["body"]), {
            "data": {"title": "修正"}, "schema_fingerprint": FINGERPRINT, "comment": "按打回原因修改"})

    def test_login_failure_has_one_post_and_does_not_disclose_credentials(self):
        self.api.reply("POST", "/api/v1/auth/login", {"error": PASSWORD}, status=401)
        self.assert_private_error(self.client.login)
        self.assertEqual(len(self.calls("POST", "/api/v1/auth/login")), 1)

    def test_unsafe_requests_are_not_retried_on_server_errors(self):
        self.api.reply("POST", "/api/v1/probe", {"error": PASSWORD}, status=503)
        self.assert_private_error(lambda: self.client.request("POST", "/probe", json={"a": 1}))
        self.assertEqual(len(self.calls("POST", "/api/v1/probe")), 1)

    def test_success_status_still_requires_valid_json_object(self):
        for payload in (b"not-json", [1, 2], None):
            with self.subTest(payload=payload):
                self.api.reply("GET", "/api/v1/probe", payload)
                self.assert_private_error(lambda: self.client.request("GET", "/probe"))

    def test_absolute_request_url_cannot_override_origin(self):
        self.assert_private_error(lambda: self.client.request("GET", "https://other-origin.invalid/probe"))
        self.assertEqual(self.calls(), [])

    def test_unsafe_api_paths_are_rejected_before_network(self):
        for path in ("//other-origin.invalid/probe", "/../probe", "/%2e%2e/probe",
                     "/probe?embedded=query", "/probe#fragment", "/probe\\other", "/probe\r\nX-Test: bad"):
            with self.subTest(path=path):
                self.assert_private_error(lambda: self.client.request("GET", path))
        self.assertEqual(self.calls(), [])

    def test_caller_cannot_override_credential_bearing_headers(self):
        for name in ("Authorization", "Cookie", "Host", "Origin", "Referer", "x-csrf-token"):
            with self.subTest(header=name):
                self.assert_private_error(lambda: self.client.request("GET", "/probe", headers={name: "bad"}))
        self.assertEqual(self.calls(), [])

    def test_json_and_stream_cannot_both_be_sent(self):
        self.assert_private_error(lambda: self.client.request("POST", "/probe", json={}, data=b"bytes"))
        self.assertEqual(self.calls(), [])

    def test_non_json_values_are_rejected_before_sending(self):
        for payload in ({"a": object()}, {"a": float("nan")}):
            with self.subTest(payload=type(payload["a"]).__name__):
                self.assert_private_error(lambda: self.client.request("POST", "/probe", json=payload))
        self.assertEqual(self.calls(), [])

    def test_connection_error_does_not_retry_or_expose_transport_details(self):
        with patch.object(self.client._opener, "open", side_effect=URLError(PASSWORD)) as opened:
            self.assert_private_error(lambda: self.client.request("POST", "/probe", json={"value": 1}))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.calls(), [])

    def test_cross_origin_redirect_never_reaches_the_second_server(self):
        other = LocalAPI()
        self.addCleanup(other.close)
        self.client.login()
        self.api.reply("GET", "/api/v1/probe", b"", status=302,
                       headers=[("Location", other.base_url + "/stolen")])
        self.assert_private_error(lambda: self.client.request("GET", "/probe"))
        self.assertEqual(other.requests, [])

    def test_post_redirect_does_not_reissue_the_submission(self):
        other = LocalAPI()
        self.addCleanup(other.close)
        self.client.login()
        self.api.reply("POST", "/api/v1/submissions", b"", status=307,
                       headers=[("Location", other.base_url + "/stolen")])
        self.assert_private_error(lambda: self.client.create({"title": "示例"}, FINGERPRINT))
        self.assertEqual(len(self.calls("POST", "/api/v1/submissions")), 1)
        self.assertEqual(other.requests, [])

    def test_same_origin_post_302_does_not_become_a_get_request(self):
        self.client.login()
        self.api.reply("POST", "/api/v1/submissions", b"", status=302,
                       headers=[("Location", self.api.base_url + "/api/v1/redirected")])
        self.assert_private_error(lambda: self.client.create({"title": "示例"}, FINGERPRINT))
        self.assertEqual(len(self.calls("POST", "/api/v1/submissions")), 1)
        self.assertEqual(self.calls(path="/api/v1/redirected"), [])
        self.assertEqual(self.calls("GET"), [])

    def test_decoded_crlf_csrf_is_rejected_without_disclosing_the_token(self):
        encoded_token = "offline-csrf%0D%0AX-Fake%3A%20" + PASSWORD
        decoded_token = "offline-csrf\r\nX-Fake: " + PASSWORD
        self.api.reply("POST", "/api/v1/auth/login", {"user": {"username": USERNAME}}, headers=[
            ("Set-Cookie", "solo_qa_session=offline-session; Path=/; HttpOnly"),
            ("Set-Cookie", "solo_qa_csrf=" + encoded_token + "; Path=/"),
        ])
        self.client.login()
        error = self.assert_private_error(lambda: self.client.request("POST", "/probe", json={"a": 1}))
        self.assertNotIn(encoded_token, str(error))
        self.assertNotIn(decoded_token, str(error))
        self.assertEqual(self.calls(path="/api/v1/probe"), [])


class Upload(HTTPCase):
    def setUp(self):
        super().setUp()
        self.file = self.home / "录屏与轨迹.bin"
        self.raw = b"offline artifact\x00\xff\n"
        self.file.write_bytes(self.raw)

    def multipart(self, request):
        content_type = request["headers"].get("Content-Type", "")
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + request["body"])
        self.assertTrue(message.is_multipart())
        return {part.get_param("name", header="Content-Disposition"): part
                for part in message.iter_parts()}

    def test_attachment_upload_sends_exact_bytes_without_kind_field(self):
        response = {"name": self.file.name, "path": "/uploads/offline.bin", "size": len(self.raw)}
        self.api.reply("POST", "/api/v1/submissions/upload", response)
        self.assertEqual(self.client.upload(self.file), response)
        request, = self.calls("POST", "/api/v1/submissions/upload")
        parts = self.multipart(request)
        self.assertEqual(set(parts), {"file"})
        self.assertEqual(parts["file"].get_payload(decode=True), self.raw)
        self.assertEqual(parts["file"].get_filename(), self.file.name)
        self.assertEqual(len(self.calls("POST", "/api/v1/auth/login")), 1)

    def test_expected_size_or_hash_mismatch_makes_no_network_requests(self):
        digest = hashlib.sha256(self.raw).hexdigest()
        for expected in ({"expected_size": len(self.raw) + 1, "expected_sha256": digest},
                         {"expected_size": len(self.raw), "expected_sha256": "0" * 64}):
            with self.subTest(expected=expected):
                self.assert_private_error(lambda: self.client.upload(self.file, **expected))
        self.assertEqual(self.calls(), [])

    def test_matching_expected_size_and_hash_upload_successfully(self):
        response = {"name": self.file.name, "path": "/uploads/verified.bin", "size": len(self.raw)}
        self.api.reply("POST", "/api/v1/submissions/upload", response)
        result = self.client.upload(self.file, expected_size=len(self.raw),
                                    expected_sha256=hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result, response)
        request, = self.calls("POST", "/api/v1/submissions/upload")
        self.assertEqual(self.multipart(request)["file"].get_payload(decode=True), self.raw)

    def test_same_size_mid_upload_change_rejects_before_multipart_suffix(self):
        raw = b"A" * (4 * 4096)
        self.file.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.client.login()
        emitted, suffixes = [], []

        def consume_upload(method, path, **kwargs):
            self.assertEqual((method, path), ("POST", "/submissions/upload"))
            body = kwargs["data"]
            suffixes.append(body.suffix)
            # Keep the same open FD, but remove buffered read-ahead from this
            # controlled sender so the disk rewrite must affect emitted bytes.
            position = body.stream.tell()
            body.stream = io.FileIO(body.stream.fileno(), "rb", closefd=False)
            body.stream.seek(position)
            iterator = iter(body)
            emitted.append(next(iterator))  # multipart prefix
            emitted.append(next(iterator))  # first file block
            with self.file.open("r+b", buffering=0) as writer:
                writer.seek(4096)
                writer.write(b"B" * (len(raw) - 4096))
            self.assertEqual(self.file.stat().st_size, len(raw))
            try:
                emitted.extend(iterator)
            finally:
                sent = b"".join(chunk for chunk in emitted[1:] if chunk != body.suffix)
                self.assertEqual(sent, raw[:4096] + b"B" * (len(raw) - 4096))
                self.assertNotEqual(sent, raw)
            return {"name": self.file.name, "path": "/uploads/mutated.bin", "size": len(raw)}

        with patch.object(platform, "CHUNK_BYTES", 4096):
            with patch.object(self.client, "request", side_effect=consume_upload) as requested:
                self.assert_private_error(lambda: self.client.upload(
                    self.file, expected_size=len(raw), expected_sha256=digest))
        self.assertEqual(requested.call_count, 1)
        self.assertEqual(len(suffixes), 1)
        self.assertNotIn(suffixes[0], emitted)

    def test_video_upload_includes_kind_and_preserves_raw_response(self):
        response = {"name": "offline.mp4", "url": "https://media.example.invalid/offline.mp4"}
        self.api.reply("POST", "/api/v1/submissions/upload", response)
        self.assertEqual(self.client.upload(self.file, kind="video"), response)
        request, = self.calls("POST", "/api/v1/submissions/upload")
        parts = self.multipart(request)
        self.assertEqual(set(parts), {"file", "kind"})
        self.assertEqual(parts["kind"].get_payload(decode=True), b"video")
        self.assertEqual(parts["file"].get_payload(decode=True), self.raw)

    def test_attachment_success_needs_name_path_and_nonnegative_integer_size(self):
        for response in ({"name": "a", "path": "/a"}, {"name": "", "path": "/a", "size": 1},
                         {"name": "a", "path": "", "size": 1}, {"name": "a", "path": "/a", "size": -1},
                         {"name": "a", "path": "/a", "size": "1"},
                         {"name": "a", "path": "/a", "size": True}):
            with self.subTest(response=response):
                self.api.reply("POST", "/api/v1/submissions/upload", response)
                self.assert_private_error(lambda: self.client.upload(self.file))

    def test_zero_byte_attachment_is_valid_without_guessed_size_limit(self):
        self.file.write_bytes(b"")
        response = {"name": self.file.name, "path": "/uploads/empty.bin", "size": 0}
        self.api.reply("POST", "/api/v1/submissions/upload", response)
        self.assertEqual(self.client.upload(self.file), response)

    def test_video_success_needs_nonempty_name_and_url(self):
        for response in ({"name": "a"}, {"name": "", "url": "/a"}, {"name": "a", "url": ""}):
            with self.subTest(response=response):
                self.api.reply("POST", "/api/v1/submissions/upload", response)
                self.assert_private_error(lambda: self.client.upload(self.file, kind="video"))

    def test_upload_post_is_not_retried_after_server_error(self):
        self.api.reply("POST", "/api/v1/submissions/upload", {"error": "offline failure"}, status=503)
        self.assert_private_error(lambda: self.client.upload(self.file))
        self.assertEqual(len(self.calls("POST", "/api/v1/submissions/upload")), 1)

    def test_unsafe_upload_sources_are_rejected_before_login_or_upload(self):
        symlink = self.home / "linked.bin"
        symlink.symlink_to(self.file)
        for path in (symlink, self.home, self.home / "missing.bin"):
            with self.subTest(path=path.name):
                self.assert_private_error(lambda: self.client.upload(path))
        self.assertEqual(self.calls(), [])

    def test_unknown_upload_kind_is_rejected_before_network(self):
        self.assert_private_error(lambda: self.client.upload(self.file, kind="unknown"))
        self.assertEqual(self.calls(), [])


class MultipartStreaming(unittest.TestCase):
    def test_file_reads_are_bounded_and_split_into_multiple_chunks(self):
        class GuardedStream(io.BytesIO):
            def __init__(self, raw):
                super().__init__(raw)
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                if size < 0 or size > 4:
                    raise AssertionError("上传不得无界读取文件")
                return super().read(size)

        raw = b"0123456789"
        stream = GuardedStream(raw)
        with patch.object(platform, "CHUNK_BYTES", 4):
            body = platform._Multipart(stream, "offline.bin", len(raw), "attachment")
            chunks = list(body)
        file_chunks = chunks[1:-1]
        self.assertGreaterEqual(len(file_chunks), 2)
        self.assertEqual(b"".join(file_chunks), raw)
        self.assertTrue(all(0 < len(chunk) <= 4 for chunk in file_chunks))
        self.assertTrue(all(0 < size <= 4 for size in stream.read_sizes))
        self.assertNotIn(-1, stream.read_sizes)
        self.assertEqual(body.length, len(b"".join(chunks)))


if __name__ == "__main__":
    unittest.main()
