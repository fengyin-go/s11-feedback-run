"""Web 录屏：真实 PTY 启动输出与同一视频中的页面操作，不改产物或启动命令。"""
from __future__ import annotations
import os
from pathlib import Path
import pty
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


def validate_plan(plan):
    if not isinstance(plan.get('serve_cmd'), str) or not plan['serve_cmd'].strip():
        raise lib.FbrError('Web 录屏方案缺少 serve_cmd')
    if not isinstance(plan.get('steps'), list) or not plan['steps']:
        raise lib.FbrError('Web 录屏方案必须给出页面 steps')
    if any(not isinstance(path, str) or not path.startswith('/') or path.startswith('//') for path in plan.get('health_paths', [])):
        raise lib.FbrError('health_paths 只能为当前服务的绝对路径')
    for step in plan['steps']:
        if not isinstance(step, dict) or step.get('action') not in ('click', 'fill', 'select', 'press', 'wait_for', 'expect_text'):
            raise lib.FbrError('Web steps 仅支持 click/fill/select/press/wait_for/expect_text')
        if step['action'] == 'select' and not isinstance(step.get('value'), str):
            raise lib.FbrError('select 必须提供原生选项的字符串 value')
        if not isinstance(step.get('selector'), str) or not step['selector']:
            raise lib.FbrError('每个页面动作必须指定 selector')
        pause = step.get('pause_ms', 300)
        if type(pause) is not int or not 0 <= pause <= 10000:
            raise lib.FbrError('pause_ms 必须是 0–10000 的整数毫秒数')


def render(plan, container, port, out_dir, rec):
    from playwright.sync_api import sync_playwright
    from fbr_record import PAGE, ScreenBuffer, type_command
    validate_plan(plan)
    url = plan.get('url', 'http://127.0.0.1:{port}/').replace('{port}', str(port))
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != port:
        raise lib.FbrError('Web 演示 URL 必须指向本次容器的 127.0.0.1:{port}')
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
    input_seconds = 0
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
            startup_budget = min(float(plan.get('startup_timeout', 45)), rec['max_seconds'] - 15)
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
                    if plan.get('ready_selector'):
                        page.locator(plan['ready_selector']).wait_for(timeout=5000)
                    for item in plan['steps']:
                        remaining = rec['max_seconds'] - 3 - (time.monotonic() - video_began)
                        if remaining < 2:
                            steps.append({'cmd': item['action'], 'rc': None, 'cut': True, 'skipped': True, 'tail': '页面演示预算不足'})
                            continue
                        action, locator = item['action'], page.locator(item['selector'])
                        wait = min(5000, int(remaining * 1000))
                        try:
                            if action == 'click':
                                locator.click(timeout=wait)
                            elif action == 'fill':
                                locator.fill(str(item.get('value', '')), timeout=wait)
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
                            steps.append({'cmd': action + ' ' + item['selector'], 'rc': 0, 'cut': False,
                                          'skipped': False, 'tail': observed})
                        except Exception as exc:
                            action_errors.append(str(exc)[:1500])
                            steps.append({'cmd': action + ' ' + item['selector'], 'rc': 1, 'cut': False,
                                          'skipped': False, 'tail': str(exc)[:1500]})
                        # 关键结果可停留供观看；始终预留封装视频所需时间。
                        pause = min(item.get('pause_ms', 300), max(0, int(
                            (rec['max_seconds'] - 3 - (time.monotonic() - video_began)) * 1000)))
                        steps[-1].update(url=page.url, pause_ms=pause)
                        page.wait_for_timeout(pause)
                except Exception as exc:
                    errors.append(str(exc)[:1500])
                page.wait_for_timeout(max(0, min(8000 - (time.monotonic() - page_began) * 1000,
                                                (rec['max_seconds'] - 2 - (time.monotonic() - video_began)) * 1000)))
                page.screenshot(path=str(out_dir / "page.png"), full_page=True)
                page_seconds = time.monotonic() - page_began
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
    return steps, {'mode': 'web', 'startup_ready': ready, 'startup_seconds': startup_end,
                   'command_input_seconds': input_seconds,
                   'page_segment': page_segment, 'page_seconds': page_seconds, 'page_errors': errors, 'action_errors': action_errors,
                   'status': 'startup_failed' if not ready else 'page_error' if errors or action_errors else 'observed'}
