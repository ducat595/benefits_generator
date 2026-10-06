#!/usr/bin/env python3
"""Standalone EDINET shareholder-benefit extraction app for Render."""
import csv
import io
import json
import os
import sys
import threading
import time
import zipfile
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import edinet_benefits

HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', '10000'))
EDINET_API_KEY = os.environ.get('EDINET_API_KEY', '').strip()
EDINET_CACHE_DIR = os.environ.get('EDINET_CACHE_DIR', '/tmp/edinet_benefits_cache')
USER_AGENT = 'EDINETBenefitsApp/1.0'
ALLOWED_ORIGINS = {'https://ducat595.github.io', 'http://localhost:10000', 'http://127.0.0.1:10000'}
ALLOWED_ORIGINS.update(x.strip().rstrip('/') for x in os.environ.get('ALLOWED_ORIGINS', '').split(',') if x.strip())
if os.environ.get('RENDER_EXTERNAL_URL'):
    ALLOWED_ORIGINS.add(os.environ['RENDER_EXTERNAL_URL'].rstrip('/'))
_hits = defaultdict(deque)
_lock = threading.Lock()
_master = None

def _edinet_get_json(url):
    if not EDINET_API_KEY:
        raise PermissionError('EDINET_API_KEYが未設定です')
    sep = '&' if '?' in url else '?'
    request = Request(url + sep + 'Subscription-Key=' + quote(EDINET_API_KEY, safe=''),
                      headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    with urlopen(request, timeout=25) as response:
        body = response.read(12 * 1024 * 1024 + 1)
    if len(body) > 12 * 1024 * 1024:
        raise ValueError('書類一覧のサイズ上限を超えました')
    data = json.loads(body.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('EDINETの応答形式が不正です')
    status = str(data.get('StatusCode') or (data.get('metadata') or {}).get('status') or '200')
    if status in ('401', '403'):
        raise PermissionError('EDINET API認証エラー')
    if status != '200':
        # Turn HTTP-200 error payloads into HTTPError for bounded retries.
        from urllib.error import HTTPError
        raise HTTPError('EDINET', int(status) if status.isdigit() else 502, 'EDINET API error', {}, None)
    return data

def _edinet_load_codelist():
    global _master
    if _master is not None:
        return _master
    url = 'https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip'
    with urlopen(Request(url, headers={'User-Agent': USER_AGENT}), timeout=30) as r:
        raw = r.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError('コードリストのサイズ上限を超えました')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(x for x in z.namelist() if x.lower().endswith('.csv'))
        if z.getinfo(name).file_size > 25 * 1024 * 1024:
            raise ValueError('コードリストの展開サイズ上限を超えました')
        text = z.read(name).decode('cp932')
    lines = text.splitlines()
    begin = next(i for i, x in enumerate(lines) if 'ＥＤＩＮＥＴコード' in x and '証券コード' in x)
    rows = {}
    for row in csv.DictReader(io.StringIO('\n'.join(lines[begin:]))):
        sec = (row.get('証券コード') or '').strip()
        if sec:
            rows[sec[:4]] = {'edinetCode': (row.get('ＥＤＩＮＥＴコード') or '').strip(),
                            'filerName': (row.get('提出者名') or '').strip(),
                            'industry': (row.get('提出者業種') or '').strip()}
    _master = rows
    return rows

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log query strings or authentication headers.
        print('[HTTP] %s %s' % (self.command, urlparse(self.path).path), flush=True)
    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(body)
    def _rate_allowed(self):
        path = urlparse(self.path).path
        cap, window = (30, 3600) if path.endswith('/job') else (120, 60)
        # Per-process global bound also limits spoofed forwarding/IP headers.
        now = time.monotonic()
        with _lock:
            q = _hits[path]
            while q and q[0] < now - window:
                q.popleft()
            if len(q) >= cap:
                self.send_json(429, {'error': 'リクエストが多すぎます。時間をおいてお試しください。'})
                return False
            q.append(now)
        return True
    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()
    def do_GET(self):
        if not self._rate_allowed():
            return
        path = urlparse(self.path).path
        if path == '/':
            path = '/edinet-benefits'
        if path == '/health':
            self.send_json(200, {'status': 'ok'})
            return
        try:
            if edinet_benefits.handle_get(self, path, sys.modules[__name__]):
                return
            self.send_json(404, {'error': 'not found'})
        except (OSError, ValueError):
            self.send_json(503, {'error': '画面または保存データを読み込めません'})
    def do_POST(self):
        if not self._rate_allowed():
            return
        if not edinet_benefits.handle_post(self, urlparse(self.path).path, sys.modules[__name__]):
            self.send_json(404, {'error': 'not found'})

if __name__ == '__main__':
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print('EDINET benefits app listening on port %d' % PORT, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
