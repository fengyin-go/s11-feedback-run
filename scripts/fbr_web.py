"""Web 录屏：真实 PTY 启动输出与同一视频中的页面操作，不改产物或启动命令。"""
from __future__ import annotations
import os
from pathlib import Path
import pty
import re
import select
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import fbrlib as lib


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def normalize_path(value):
    path = urlparse(str(value or '/')).path or '/'
    return '/' + path.strip('/') if path.strip('/') else '/'


def coverage_pages(plan, url):
    """Return declared pages, or one implicit page for a legacy plan."""
    declared = plan.get('pages')
    if declared is None:
        return [{
            'id': 'page-1',
            'path': normalize_path(url),
            'ready_selector': plan.get('ready_selector', ''),
            'min_visible_seconds': 2,
        }], False
    return [dict(item) for item in declared], True


def matched_page(current_url, pages):
    path = normalize_path(current_url)
    matches = []
    for item in pages:
        expected = normalize_path(item['path'])
        if path == expected or path.startswith(expected.rstrip('/') + '/'):
            matches.append((len(expected), item))
    return max(matches, default=(0, None))[1]


def validate_plan(plan, *, require_pages=False):
    if not isinstance(plan.get('serve_cmd'), str) or not plan['serve_cmd'].strip():
        raise lib.FbrError('Web 录屏方案缺少 serve_cmd')
    if not isinstance(plan.get('steps'), list) or not plan['steps']:
        raise lib.FbrError('Web 录屏方案必须给出页面 steps')
    if any(not isinstance(path, str) or not path.startswith('/') or path.startswith('//') for path in plan.get('health_paths', [])):
        raise lib.FbrError('health_paths 只能为当前服务的绝对路径')
    pages = plan.get('pages')
    if require_pages and (not isinstance(pages, list) or not pages):
        raise lib.FbrError('新批次 Web 录屏方案必须声明 pages 页面覆盖')
    page_ids = set()
    if pages is not None:
        if not isinstance(pages, list) or not pages:
            raise lib.FbrError('pages 必须是非空列表')
        for page in pages:
            if not isinstance(page, dict):
                raise lib.FbrError('pages 每一项必须是对象')
            page_id = page.get('id')
            if (not isinstance(page_id, str) or not PAGE_ID_RE.fullmatch(page_id)
                    or page_id in page_ids):
                raise lib.FbrError('page.id 必须唯一且只包含字母、数字、点、下划线或横线')
            page_ids.add(page_id)
            path = page.get('path')
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
                raise lib.FbrError('page.path 必须是当前服务的绝对路径')
            visible = page.get('min_visible_seconds', 2)
            if type(visible) is not int or not 1 <= visible <= 15:
                raise lib.FbrError('page.min_visible_seconds 必须是 1–15 的整数')
            selector = page.get('ready_selector', '')
            if not isinstance(selector, str):
                raise lib.FbrError('page.ready_selector 必须是字符串')
    referenced = set()
    for step in plan['steps']:
        if not isinstance(step, dict) or step.get('action') not in ('click', 'fill', 'set_files', 'select', 'press', 'wait_for', 'expect_text'):
            raise lib.FbrError('Web steps 仅支持 click/fill/set_files/select/press/wait_for/expect_text')
        if step['action'] == 'set_files':
            value = step.get('value')
            if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_file():
                raise lib.FbrError('set_files 必须提供存在的本地文件路径')
        if step['action'] == 'select' and not isinstance(step.get('value'), str):
            raise lib.FbrError('select 必须提供原生选项的字符串 value')
        if not isinstance(step.get('selector'), str) or not step['selector']:
            raise lib.FbrError('每个页面动作必须指定 selector')
        pause = step.get('pause_ms', 300)
        if type(pause) is not int or not 0 <= pause <= 10000:
            raise lib.FbrError('pause_ms 必须是 0–10000 的整数毫秒数')
        page_id = step.get('page')
        if page_ids:
            if page_id not in page_ids:
                raise lib.FbrError('声明 pages 后，每个 step.page 必须指向真实页面')
            referenced.add(page_id)
        elif page_id is not None:
            raise lib.FbrError('step.page 只能用于已声明的 pages')
    if page_ids and referenced != page_ids:
        missing = sorted(page_ids - referenced)
        raise lib.FbrError('页面缺少实际操作或结果步骤：' + '、'.join(missing))


def render(plan, container, port, out_dir, rec):
    from playwright.sync_api import sync_playwright
    from fbr_record import PAGE, ScreenBuffer, type_command
    url = plan.get('url', 'http://127.0.0.1:{port}/').replace('{port}', str(port))
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != port:
        raise lib.FbrError('Web 演示 URL 必须指向本次容器的 127.0.0.1:{port}')
    pages, coverage_declared = coverage_pages(plan, url)
    command = plan['serve_cmd'].replace('{port}', str(port)).replace('{host}', '0.0.0.0')
    size = {'width': rec['width'], 'height': rec['height']}
    screen = ScreenBuffer()
    master = process = thread = raw = None
    stop = threading.Event()
    errors, steps, action_errors = [], [], []

    def drain():
        while not stop.is_set():
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                raw.write(chunk)
                raw.flush()
                screen.feed(chunk)

    ready = False
    startup_end = 0
    page_seconds = 0
    page_segment = False
    page_ready = False
    input_seconds = 0
    visits = {
        item['id']: {
            'id': item['id'],
            'path': item['path'],
            'min_visible_seconds': item.get('min_visible_seconds', 2),
            'visited': False,
            'visible_seconds': 0.0,
            'actions': 0,
            'screenshot': '',
        }
        for item in pages
    }
    active_page = None
    active_since = 0.0
    page_screens = []
    raw = open(out_dir / 'terminal.raw.log', 'wb')
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            context = browser.new_context(viewport=size, record_video_dir=str(out_dir / 'video-tmp'), record_video_size=size)
            page = context.new_page()
            page.set_content(PAGE % {'w': rec['width'], 'h': rec['height']})
            page.screenshot()

            def tick():
                page.evaluate("t => { const o = document.getElementById('output'); o.textContent=t;"
                              " o.scrollTop = o.scrollHeight; }", screen.snapshot())

            def close_active():
                nonlocal active_page, active_since
                if active_page is not None:
                    visits[active_page]['visible_seconds'] += max(0.0, time.monotonic() - active_since)
                active_page = None
                active_since = 0.0

            def observe_page():
                nonlocal active_page, active_since
                current = matched_page(page.url, pages)
                current_id = current['id'] if current else None
                if current_id == active_page:
                    return current_id
                close_active()
                if current_id:
                    active_page = current_id
                    active_since = time.monotonic()
                    visits[current_id]['visited'] = True
                    capture_page(current_id)
                return current_id

            def capture_page(page_id):
                if not page_id or visits[page_id]['screenshot']:
                    return
                target = out_dir / 'pages'
                target.mkdir(exist_ok=True)
                screenshot = target / f'{page_id}.png'
                page.screenshot(path=str(screenshot), full_page=True)
                visits[page_id]['screenshot'] = str(screenshot.name)
                page_screens.append(str(screenshot.name))

            def hold_page(page_id):
                if not page_id:
                    return
                minimum = float(visits[page_id]['min_visible_seconds'])
                elapsed = visits[page_id]['visible_seconds']
                if active_page == page_id:
                    elapsed += time.monotonic() - active_since
                remaining = minimum - elapsed
                if remaining > 0:
                    budget = rec['max_seconds'] - 3 - (time.monotonic() - video_began)
                    page.wait_for_timeout(int(min(remaining, max(0.0, budget)) * 1000))
                    observe_page()

            # Recording begins with the operator-facing prompt and the command entry itself.
            video_began = time.monotonic()
            input_seconds = type_command(screen, raw, command, tick)
            master, slave = pty.openpty()
            process = subprocess.Popen(
                ['docker', 'exec', '-it', '-w', '/workspace', container, 'bash', '-lc', command],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            )
            os.close(slave)
            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            began = time.monotonic()
            page_floor = float(rec.get('page_min_seconds', 12))
            startup_budget = min(
                float(plan.get('startup_timeout', 45)),
                max(10.0, rec['max_seconds'] - page_floor - 5),
            )
            while time.monotonic() - began < startup_budget:
                tick()
                if not ready:
                    try:
                        # 不受宿主代理影响，只请求本次映射的本地端口。
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        checks = [url] + [f"http://127.0.0.1:{port}{path}" for path in plan.get('health_paths', [])]
                        ready = True
                        for check in checks:
                            with opener.open(check, timeout=.5) as response:
                                ready = ready and 200 <= response.status < 400
                    except (OSError, urllib.error.URLError):
                        ready = False
                elapsed = time.monotonic() - began
                if ready and elapsed >= 10:
                    break
                if process.poll() is not None and not ready and elapsed >= 8:
                    break
                page.wait_for_timeout(150)
            startup_end = time.monotonic() - began
            steps.append({'cmd': command, 'show': '服务启动', 'rc': process.poll() if process.poll() is not None else None,
                          'seconds': startup_end, 'cut': False, 'skipped': False, 'tail': screen.snapshot()[-3000:],
                          'startup_ready': ready, 'input_recorded': True, 'input_seconds': input_seconds})
            if ready:
                page.on('pageerror', lambda error: errors.append(str(error)))
                page_began = time.monotonic()
                try:
                    page.goto(url, timeout=10000, wait_until='domcontentloaded')
                    page_segment = True
                    page_ready = True
                    current_id = observe_page()
                    current_page = next((item for item in pages if item['id'] == current_id), None)
                    if current_page and current_page.get('ready_selector'):
                        page.locator(current_page['ready_selector']).wait_for(timeout=5000)
                    elif plan.get('ready_selector'):
                        page.locator(plan['ready_selector']).wait_for(timeout=5000)
                    capture_page(current_id)
                    for item in plan['steps']:
                        remaining = rec['max_seconds'] - 3 - (time.monotonic() - video_began)
                        if remaining < 2:
                            steps.append({'cmd': item['action'], 'page': item.get('page'), 'rc': None,
                                          'cut': True, 'skipped': True, 'tail': '页面演示预算不足'})
                            continue
                        hold_page(observe_page())
                        expected_page = item.get('page')
                        action, locator = item['action'], page.locator(item['selector'])
                        actual_page = observe_page()
                        navigates_to_expected = bool(
                            expected_page and actual_page != expected_page and action == 'click')
                        if actual_page:
                            visits[actual_page]['actions'] += 1
                        if expected_page and actual_page != expected_page and action != 'click':
                            action_errors.append(
                                f"第 {len(steps) + 1} 个页面动作期望 {expected_page}，实际为 {actual_page or '未知页面'}"
                            )
                            steps.append({
                                'cmd': item['action'] + ' ' + item['selector'],
                                'page': expected_page, 'actual_page': actual_page,
                                'rc': 1, 'cut': False, 'skipped': False,
                                'tail': '页面目标不匹配，动作未执行',
                            })
                            continue
                        wait = min(5000, int(remaining * 1000))
                        try:
                            if action == 'click':
                                locator.click(timeout=wait)
                                if navigates_to_expected:
                                    target = next(item for item in pages if item['id'] == expected_page)
                                    page.wait_for_url(f"**{target['path']}**", timeout=wait)
                            elif action == 'fill':
                                locator.fill(str(item.get('value', '')), timeout=wait)
                            elif action == 'set_files':
                                locator.set_input_files(str(Path(item['value']).expanduser()), timeout=wait)
                            elif action == 'select':
                                locator.select_option(value=item['value'], timeout=wait)
                            elif action == 'press':
                                locator.press(str(item.get('key', 'Enter')), timeout=wait)
                            elif action == 'wait_for':
                                locator.wait_for(state='visible', timeout=wait)
                            else:
                                from playwright.sync_api import expect
                                expect(locator).to_contain_text(str(item.get('text', '')), timeout=wait)
                            # 成功跳页或提交后，原元素可能已卸载；可选文字采集失败不等于动作失败。
                            try:
                                observed = locator.inner_text(timeout=300)[:1500]
                            except Exception:
                                observed = ''
                            current_id = observe_page()
                            if navigates_to_expected and current_id != expected_page:
                                target = next(item for item in pages if item['id'] == expected_page)
                                raise RuntimeError(
                                    f"动作后未进入页面 {expected_page}（{target['path']}），"
                                    f"实际为 {current_id or '未知页面'}"
                                )
                            capture_page(current_id)
                            steps.append({'cmd': action + ' ' + item['selector'],
                                          'page': expected_page, 'actual_page': current_id,
                                          'rc': 0, 'cut': False, 'skipped': False, 'tail': observed})
                        except Exception as exc:
                            action_errors.append(str(exc)[:1500])
                            current_id = observe_page()
                            steps.append({'cmd': action + ' ' + item['selector'],
                                          'page': expected_page, 'actual_page': current_id,
                                          'rc': 1, 'cut': False, 'skipped': False,
                                          'tail': str(exc)[:1500]})
                        # 关键结果可停留供观看；始终预留封装视频所需时间。
                        pause = min(item.get('pause_ms', 300), max(0, int(
                            (rec['max_seconds'] - 3 - (time.monotonic() - video_began)) * 1000)))
                        steps[-1].update(url=page.url, pause_ms=pause)
                        page.wait_for_timeout(pause)
                        hold_page(observe_page())
                except Exception as exc:
                    errors.append(str(exc)[:1500])
                    page_ready = False
                observe_page()
                hold_page(active_page)
                page.wait_for_timeout(max(0, min(
                    (float(rec.get('page_min_seconds', 12)) - (time.monotonic() - page_began)) * 1000,
                    (rec['max_seconds'] - 2 - (time.monotonic() - video_began)) * 1000,
                )))
                observe_page()
                page.screenshot(path=str(out_dir / "page.png"), full_page=True)
                page_seconds = time.monotonic() - page_began
                close_active()
            else:
                page.wait_for_timeout(500)
            video = page.video
            context.close()
            video.save_as(str(out_dir / 'raw.webm'))
            browser.close()
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=2)
        if raw is not None:
            raw.close()
        if master is not None:
            os.close(master)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    page_rows = list(visits.values())
    missing_pages = [
        row['id'] for row in page_rows
        if not row['visited'] or row['visible_seconds'] + 0.05 < row['min_visible_seconds']
    ]
    execution_complete = not any(
        step.get('skipped') or step.get('cut') for step in steps
    )
    coverage_complete = (
        page_ready and execution_complete and not missing_pages
        and all(row['actions'] > 0 for row in page_rows)
    )
    return steps, {
        'mode': 'web',
        'startup_ready': ready,
        'startup_seconds': startup_end,
        'command_input_seconds': input_seconds,
        'page_segment': page_segment,
        'page_ready': page_ready,
        'page_seconds': page_seconds,
        'page_errors': errors,
        'action_errors': action_errors,
        'coverage_declared': coverage_declared,
        'coverage_complete': coverage_complete,
        'missing_pages': missing_pages,
        'execution_complete': execution_complete,
        'pages': page_rows,
        'page_screenshots': page_screens,
        'status': (
            'startup_failed' if not ready
            else 'page_failed' if not page_ready
            else 'page_incomplete' if not coverage_complete
            else 'page_error' if errors or action_errors
            else 'observed'
        ),
    }
