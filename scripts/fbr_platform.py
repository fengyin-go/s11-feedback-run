#!/usr/bin/env python3
"""SOLO-QA transport only: fixed host, in-memory auth, JSON and streamed uploads.

No CLI, submission policy, receipt storage or automatic POST retries live here.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import hashlib
import json as jsonlib
import mimetypes
import os
import re
import stat
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import fbrlib as lib

BASE_URL = "https://solo2.jzxhnh.com"
API_PREFIX = "/api/v1"
CSRF_COOKIE = "solo_qa_csrf"
CSRF_HEADER = "X-CSRF-Token"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class PlatformError(lib.FbrError):
    """Sanitized transport/auth error; never includes remote bodies or secrets."""


class RequestNotSent(PlatformError):
    """TLS negotiation failed before HTTP bytes could be sent."""


def tls_before_http(exc: BaseException) -> bool:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if not isinstance(reason, ssl.SSLError):
        return False
    frames = []
    tb = reason.__traceback__
    while tb:
        frames.append((tb.tb_frame.f_globals.get('__name__'), tb.tb_frame.f_code.co_name))
        tb = tb.tb_next
    return ('ssl', 'do_handshake') in frames and ('http.client', 'connect') in frames


def _credentials_file(path: Path) -> dict:
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise PlatformError("平台凭据必须是权限 600 的普通文件")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = jsonlib.load(stream)
    except PlatformError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise PlatformError("无法安全读取平台凭据文件") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise PlatformError("平台凭据格式不正确")
    username, password = value.get("username"), value.get("password")
    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
        raise PlatformError("平台凭据缺少 username 或 password")
    # Deliberately discard old base_url and all unrelated credential fields.
    return {"username": username, "password": password}


def load_credentials(account: str = "gonghao") -> dict:
    if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", account) or account in (".", ".."):
        raise PlatformError("平台账号名称格式不正确")
    directory = Path.home() / ".codex" / "coding-agent-satisfaction"
    primary = directory / "solo-qa-accounts" / f"{account}.json"
    # A broken or unsafe primary must fail closed, never silently use fallback.
    if primary.exists() or primary.is_symlink():
        credentials = _credentials_file(primary)
        if credentials["username"] != account:
            raise PlatformError("平台凭据的 username 与指定账号不一致")
        return credentials
    fallback = directory / "solo-qa.json"
    if not fallback.exists() and not fallback.is_symlink():
        raise PlatformError("没有本账号的平台凭据文件")
    credentials = _credentials_file(fallback)
    if credentials["username"] != account:
        raise PlatformError("备用平台凭据的 username 与指定账号不一致")
    return credentials


def _origin(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise PlatformError("平台请求地址不允许包含凭据")
    return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class _BoundRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin: tuple):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != self.origin:
            raise PlatformError("平台请求被重定向到其他 origin，已拒绝")
        if req.get_method() not in ("GET", "HEAD"):
            raise PlatformError("平台写请求发生重定向，未重发；结果需核查")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Multipart:
    """Single-use iterable; the file is never read into a whole-file buffer."""

    def __init__(self, stream, name: str, size: int, kind: str, expected_sha256: str | None = None):
        self.stream, self.size = stream, size
        self.expected_sha256 = expected_sha256
        self.boundary = "solo11-" + uuid.uuid4().hex
        safe_name = name.replace("\r", "_").replace("\n", "_").replace('"', "_").replace("\\", "_")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        prefix = b""
        if kind == "video":
            prefix += (f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nvideo\r\n").encode()
        prefix += (f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
                   f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        self.prefix = prefix
        self.suffix = f"\r\n--{self.boundary}--\r\n".encode()
        self.length = len(prefix) + size + len(self.suffix)
        self.used = False

    def __iter__(self):
        if self.used:
            raise PlatformError("上传流不能重复发送")
        self.used = True
        yield self.prefix
        remaining = self.size
        checksum = hashlib.sha256()
        while remaining:
            block = self.stream.read(min(CHUNK_BYTES, remaining))
            if not block:
                raise PlatformError("上传文件在读取时发生变化，结果需核查")
            remaining -= len(block)
            checksum.update(block)
            yield block
        if self.stream.read(1):
            raise PlatformError("上传文件在读取时发生变化，结果需核查")
        if self.expected_sha256 is not None and checksum.hexdigest() != self.expected_sha256:
            raise PlatformError("上传文件在发送时发生变化，未发送结束边界，结果需核查")
        yield self.suffix


class Client:
    def __init__(self, account: str = "gonghao", *, timeout: float = 120):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise PlatformError("平台请求 timeout 必须是正数")
        self.account = account
        self.timeout = timeout
        self._base = BASE_URL.rstrip("/")
        self._origin = _origin(self._base)
        self._cookies = http.cookiejar.CookieJar()
        # Do not hand credential-bearing requests to environment-selected proxies.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self._cookies),
            _BoundRedirect(self._origin))
        self._logged_in = False
        self._login_result = None

    def _url(self, path: str, params: dict | None) -> str:
        if not isinstance(path, str) or any(ord(c) < 32 for c in path):
            raise PlatformError("平台 API 路径不合法")
        parts = urllib.parse.urlsplit(path)
        decoded = urllib.parse.unquote(parts.path)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or not path.startswith("/") or "\\" in path:
            raise PlatformError("平台请求只接受本站 API 相对路径")
        if any(p in (".", "..") for p in decoded.split("/")):
            raise PlatformError("平台 API 路径不能跨目录")
        normalized = path if path == API_PREFIX or path.startswith(API_PREFIX + "/") else API_PREFIX + path
        url = self._base + normalized
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url

    def request(self, method: str, path: str, *, json=None, params=None, headers=None, data=None) -> dict:
        method = method.upper()
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            raise PlatformError("不支持的平台 HTTP 方法")
        if json is not None and data is not None:
            raise PlatformError("平台请求不能同时发送 JSON 与上传流")
        url = self._url(path, params)
        request_headers = {"Accept": "application/json", "Origin": self._base, "Referer": self._base + "/"}
        for name, value in (headers or {}).items():
            if name.lower() in ("authorization", "cookie", "host", "origin", "referer", CSRF_HEADER.lower()):
                raise PlatformError("平台认证请求头不能由调用方覆盖")
            request_headers[name] = value
        if json is not None:
            try:
                data = jsonlib.dumps(json, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise PlatformError("平台请求数据无法编码为 JSON") from None
            request_headers["Content-Type"] = "application/json"
        try:
            req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
            self._cookies.add_cookie_header(req)
            if method not in ("GET", "HEAD"):
                # Use the cookie selected for this request, not another path/domain.
                cookie = req.get_header("Cookie", "")
                for part in cookie.split(";"):
                    name, sep, value = part.strip().partition("=")
                    if sep and name == CSRF_COOKIE:
                        req.add_header(CSRF_HEADER, urllib.parse.unquote(value))
                        break
            # One open call: write operations are never retried by this layer.
            with self._opener.open(req, timeout=self.timeout) as response:
                if _origin(response.geturl()) != self._origin:
                    raise PlatformError("平台响应来自其他 origin，已拒绝")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise PlatformError("平台响应超过安全读取上限")
        except PlatformError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise PlatformError(f"平台 HTTP {code}；未自动重试，写请求结果需核查") from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, ValueError) as exc:
            if tls_before_http(exc):
                raise RequestNotSent("平台TLS握手失败，HTTP请求尚未发送；未自动重试") from None
            raise PlatformError("平台连接失败或超时；未自动重试，写请求结果需核查") from None
        try:
            value = jsonlib.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise PlatformError("平台响应不是有效 JSON 对象") from None
        if not isinstance(value, dict):
            raise PlatformError("平台响应不是 JSON 对象")
        return value

    def login(self) -> dict:
        if self._logged_in:
            return dict(self._login_result)
        credentials = load_credentials(self.account)
        self._logged_in = False
        result = self.request("POST", "/auth/login", json=credentials)
        self._login_result = dict(result)
        self._logged_in = True
        return result

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def me(self) -> dict:
        self._ensure_login()
        return self.request("GET", "/auth/me")

    def schema(self) -> dict:
        self._ensure_login()
        return self.request("GET", "/submissions/form-schema")

    def list_submissions(self, page=1, page_size=100, **params) -> dict:
        self._ensure_login()
        return self.request("GET", "/submissions", params={**params, "page": page, "page_size": page_size})

    def detail(self, id) -> dict:
        if isinstance(id, bool) or not re.fullmatch(r"[1-9][0-9]*", str(id)):
            raise PlatformError("平台记录 ID 必须是正整数")
        self._ensure_login()
        return self.request("GET", f"/submissions/{id}")

    def create(self, data: dict, fingerprint: str) -> dict:
        self._ensure_login()
        return self.request("POST", "/submissions", json={"data": data, "schema_fingerprint": fingerprint})

    def upload(self, path: Path, kind: str = "attachment", expected_size: int | None = None,
               expected_sha256: str | None = None) -> dict:
        if kind not in ("attachment", "video"):
            raise PlatformError("上传 kind 只能是 attachment 或 video")
        if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
            raise PlatformError("交付清单的附件大小必须是非负整数")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                           or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise PlatformError("交付清单的附件 SHA256 格式不正确")
        path = Path(path)
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise PlatformError("上传源必须是普通文件，不能是符号链接")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                if expected_size is not None and info.st_size != expected_size:
                    raise PlatformError("上传文件大小与交付清单不一致")
                checksum, count = hashlib.sha256(), 0
                while block := stream.read(CHUNK_BYTES):
                    checksum.update(block)
                    count += len(block)
                before_sha = checksum.hexdigest()
                if count != info.st_size or os.fstat(stream.fileno()).st_size != info.st_size:
                    raise PlatformError("上传文件在校验时发生变化")
                if expected_sha256 is not None and before_sha != expected_sha256:
                    raise PlatformError("上传文件 SHA256 与交付清单不一致")
                stream.seek(0)
                body = _Multipart(stream, path.name, info.st_size, kind, before_sha)
                self._ensure_login()
                result = self.request("POST", "/submissions/upload", data=body, headers={
                    "Content-Type": f"multipart/form-data; boundary={body.boundary}", "Content-Length": str(body.length)})
        except PlatformError:
            raise
        except OSError:
            raise PlatformError("无法安全读取上传文件") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not isinstance(result.get("name"), str) or not result["name"].strip():
            raise PlatformError("上传响应缺少有效文件名，未确认上传成功")
        if kind == "video":
            valid = isinstance(result.get("url"), str) and bool(result["url"].strip())
        else:
            valid = (isinstance(result.get("path"), str) and bool(result["path"].strip())
                     and type(result.get("size")) is int and result["size"] >= 0)
        if not valid:
            raise PlatformError("上传响应缺少有效附件信息，未确认上传成功")
        return result
