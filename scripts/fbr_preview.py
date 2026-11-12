#!/usr/bin/env python3
"""本地抽检预览服务，支持视频 Range；只暴露本批交付目录。"""
from __future__ import annotations
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

import fbrlib as lib
import fbr_runtime as runtime


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        self.byte_range = None
        root = Path(self.directory).resolve()
        path = Path(self.translate_path(self.path)).resolve()
        if not path.is_relative_to(root):
            self.send_error(403)
            return None
        if path.is_dir() or 'Range' not in self.headers:
            return super().send_head()
        try:
            stream = path.open('rb')
        except OSError:
            self.send_error(404)
            return None
        size = os.fstat(stream.fileno()).st_size
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers['Range'])
        if match and any(match.groups()):
            left, right = match.groups()
            start = int(left) if left else max(0, size - int(right))
            end = min(size - 1, int(right)) if left and right else size - 1
        else:
            start, end = size, -1
        if not 0 <= start <= end < size:
            stream.close()
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        self.byte_range = (start, end)
        stream.seek(start)
        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(str(path)))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        return stream

    def copyfile(self, source, outputfile):
        if self.byte_range is None:
            return super().copyfile(source, outputfile)
        remaining = self.byte_range[1] - self.byte_range[0] + 1
        while remaining:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def log_message(self, *args):
        pass


def serve(root, batch, port):
    from fbr_delivery import delivery_dir
    directory = delivery_dir(root, batch)
    control = lib.batch_dir(root, batch) / 'flow'
    with lib.file_lock(control / 'preview.lock'):
        with ThreadingHTTPServer(('127.0.0.1', port), partial(Handler, directory=str(directory))) as server:
            lib.save_json(control / 'preview-server.json', {'pid': os.getpid(), 'identity': runtime.process_identity(),
                          'port': server.server_port, 'url': f'http://127.0.0.1:{server.server_port}/preview.html'})
            server.serve_forever()


def stop(root, batch):
    import signal
    from fbr_delivery import delivery_dir
    path = lib.batch_dir(root, batch) / 'flow/preview-server.json'
    state = lib.load_json(path, default={})
    if runtime.alive(state):
        os.kill(state['pid'], signal.SIGTERM)
    path.unlink(missing_ok=True)
    return {'state': 'stopped'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root')
    subs = p.add_subparsers(dest='command', required=True)
    for name in ('run', 'serve', 'stop'):
        item = subs.add_parser(name)
        item.add_argument('--batch', required=True)
        item.add_argument('--port', type=int, default=8899)
        item.add_argument('--detach', action='store_true')
    args = p.parse_args()
    try:
        root = lib.find_root(args.root)
        if args.command == 'stop':
            result = stop(root, args.batch)
        elif args.command == 'serve':
            serve(root, args.batch, args.port)
            return 0
        else:
            from fbr_batch import preview
            path = preview(root, args.batch)
            if args.detach:
                control = lib.batch_dir(root, args.batch) / 'flow'
                control.mkdir(parents=True, exist_ok=True)
                state = lib.load_json(control / 'preview-server.json', default={})
                if not runtime.alive(state):
                    argv = [sys.executable, str(Path(__file__).resolve()), '--root', str(root),
                            'serve', '--batch', args.batch, '--port', str(args.port)]
                    with (control / 'preview-server.log').open('ab') as log:
                        child = subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
                    result = {'pid': child.pid, 'url': f'http://127.0.0.1:{args.port}/preview.html'}
                else:
                    result = state
            else:
                serve(root, args.batch, args.port)
                return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (lib.FbrError, OSError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
