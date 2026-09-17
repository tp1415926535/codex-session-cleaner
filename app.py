"""Run with Python 3.10+. No third-party packages required."""
from __future__ import annotations

import argparse
import hashlib
import ctypes
import hmac
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cleaner.core import Store

ROOT = Path(__file__).resolve().parent


def make_server(store, port=8765):
    csrf = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Avoid logging session content, ids or query strings.
            pass

        def reply(self, data, status=200, mime='application/json; charset=utf-8'):
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8') if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host(): return self.reply({'error': 'Host 校验失败'}, 403)
            origin = self.headers.get('Origin')
            if origin and origin != f'http://127.0.0.1:{self.server.server_port}':
                return self.reply({'error': 'Origin 校验失败'}, 403)
            path = urlsplit(self.path)
            try:
                if path.path == '/api/database-inspect': return self.reply(store.databases.inspect())
                if path.path == '/api/database-progress': return self.reply(store.databases.progress)
                if path.path == '/api/delete-progress':
                    return self.reply({**store.delete_progress, 'result': store.delete_result if not store.delete_progress['running'] else None})
                if path.path == '/api/state':
                    return self.reply({**store.snapshot(), 'csrf': csrf})
                if path.path == '/api/preview':
                    q = parse_qs(path.query)
                    return self.reply(store.preview(q.get('id', [''])[0], int(q.get('offset', ['0'])[0]), int(q.get('file', ['0'])[0])))
                assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/i18n.js': ('i18n.js', 'text/javascript'), '/database.js': ('database.js', 'text/javascript'), '/locales/zh-CN.json': ('locales/zh-CN.json', 'application/json'), '/locales/en.json': ('locales/en.json', 'application/json'), '/style.css': ('style.css', 'text/css')}
                if path.path in assets:
                    file, mime = assets[path.path]
                    return self.reply((ROOT / 'web' / file).read_bytes(), mime=mime + '; charset=utf-8')
                self.reply({'error': 'Not found'}, 404)
            except Exception as e:
                self.reply({'error': str(e)}, 400)

        def do_POST(self):
            expected = f'http://127.0.0.1:{self.server.server_port}'
            if not self.valid_host() or self.headers.get('Origin') != expected or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), csrf):
                return self.reply({'error': '本地来源或 CSRF 校验失败，请从工具页面操作'}, 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 32768: raise ValueError('请求过大')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise ValueError('仅支持 JSON 请求')
                data = json.loads(self.rfile.read(length))
                if self.path == '/api/database-plan': return self.reply(store.databases.plan(data.get('names'), data.get('log_days')))
                if self.path == '/api/database-start':
                    if data.get('confirmed') is not True: raise ValueError('Confirmation required')
                    return self.reply(store.databases.start(data.get('token', '')))
                if self.path == '/api/database-stop': return self.reply(store.databases.cancel())
                if self.path == '/api/scan':
                    store.start_scan(); return self.reply({'ok': True})
                if self.path == '/api/cancel':
                    store.cancel.set(); return self.reply({'ok': True})
                if self.path == '/api/plan': return self.reply(store.plan(data.get('ids', [])))
                if self.path == '/api/delete-stop': return self.reply(store.stop_delete())
                if self.path == '/api/delete':
                    if data.get('confirmed') is not True: raise ValueError('请在删除计划中点击确认删除')
                    return self.reply(store.delete(data.get('token', '')))
                if self.path == '/api/settings':
                    if not store.operation.acquire(False): raise ValueError('删除中不能修改设置')
                    try:
                        store.cancel.set()
                        if store.worker: store.worker.join(10)
                        if store.worker and store.worker.is_alive(): raise ValueError('扫描尚未停止')
                        home = Path(data.get('home', '')).expanduser().resolve()
                        if not home.is_dir(): raise ValueError('数据目录不存在')
                        store.home = home
                        store.cli_override = data.get('cli', '').strip()
                        store.rows = {}
                        store.header_cache.clear()
                        store.complete = False
                        store.plans.clear()
                    finally: store.operation.release()
                    store.start_scan()
                    return self.reply({'ok': True})
                self.reply({'error': 'Not found'}, 404)
            except Exception as e:
                self.reply({'error': str(e)}, 400)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    try:
        return Server(('127.0.0.1', port), Handler)
    except OSError:
        if port == 0: raise
        return Server(('127.0.0.1', 0), Handler)


def main():
    p = argparse.ArgumentParser(description='Codex Session Cleaner · 本地会话管理')
    p.add_argument('--home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    p.add_argument('--cli', default=os.environ.get('CODEX_CLEANER_CLI', ''))
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--no-browser', action='store_true')
    args = p.parse_args()
    # One desktop service per data directory, regardless of HTTP port.
    mutex = None
    if os.name == 'nt':
        key = hashlib.sha256(os.path.normcase(str(Path(args.home).resolve())).encode()).hexdigest()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, 'Local\\CodexSessionCleaner-' + key)
        if not mutex: raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            print('该数据目录的清理服务已经运行，请使用原窗口。', flush=True)
            return
    store = Store(args.home, args.cli)
    server = make_server(store, args.port)
    url = f'http://127.0.0.1:{server.server_port}'
    print('Codex Session Cleaner: ' + url + f' (PID {os.getpid()})', flush=True)
    store.start_scan()
    if not args.no_browser: threading.Timer(.5, webbrowser.open, args=(url,)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        store.cancel.set()
        server.server_close()
        if mutex: kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
