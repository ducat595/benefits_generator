"""EDINET-only benefit mention collector. Standard library; no third-party feeds."""
import copy
import io
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

JST = timezone(timedelta(hours=9))
MAX_ZIP = 30 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
KEYWORD = re.compile(r'株主\s*優待|株主\s*ご優待')
BLOCK = {'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'br', 'table'}

class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ('script', 'style', 'ix:header', 'ix:hidden'):
            self.hidden += 1
        if not self.hidden and tag in BLOCK:
            self.parts.append('\n')
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('script', 'style', 'ix:header', 'ix:hidden') and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in BLOCK:
            self.parts.append('\n')
    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)

def plain(source):
    p = Text()
    p.feed(source)
    return '\n'.join(re.sub(r'[ \t\r\f\v]+', ' ', unicodedata.normalize('NFKC', x)).strip()
                     for x in ''.join(p.parts).splitlines() if x.strip())

def snippets(text):
    """Keep source passages, not inferred benefit summaries."""
    found = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not KEYWORD.search(line):
            continue
        # Short blocks retain adjacent table rows, up to 8 following lines.
        if len(line) <= 1000:
            s = '\n'.join(lines[max(0, i - 1):i + 9])[:6000]
        else:
            # Escaped XBRL may be one long block; select a bounded context window.
            m = KEYWORD.search(line)
            s = line[max(0, m.start() - 150):m.end() + 4500]
        if s not in found:
            found.append(s)
    return found[:40]

def extract_zip(body):
    passages = []
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        entries = z.infolist()
        if sum(x.file_size for x in entries) > MAX_EXPANDED:
            raise ValueError('書類の展開サイズが上限を超えました')
        files = [x for x in entries if '/publicdoc/' in ('/' + x.filename.lower())]
        inline = [x for x in files if x.filename.lower().endswith(('.htm', '.html', '.xhtml'))]
        chosen = inline or [x for x in files if x.filename.lower().endswith('.xbrl')]
        if not chosen:
            raise ValueError('解析可能な提出本文HTML/XBRLがありません')
        for item in chosen:
            if item.file_size > MAX_FILE:
                raise ValueError('本文ファイルのサイズが上限を超えました')
            raw = z.read(item).decode('utf-8-sig')
            if inline:
                texts = [plain(raw)]
            else:
                if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
                    raise ValueError('未対応のXML宣言です')
                root = ET.fromstring(raw)
                texts = [plain(unescape(''.join(el.itertext()))) for el in root.iter()
                         if el.attrib.get('contextRef') and KEYWORD.search(''.join(el.itertext()))]
            for text in texts:
                for snippet in snippets(text):
                    if not any(p['text'] == snippet for p in passages):
                        passages.append({'file': item.filename, 'text': snippet})
    return passages

def classify(passages, correction=False):
    joined = '\n'.join(x['text'] for x in passages)
    negative = re.search(r'株主\s*(?:ご)?優待(?:制度)?(?:は|を|について|につきまして)?[^。\n]{0,50}(?:実施しておりません|実施していません|設けておりません|行っておりません|導入しておりません|廃止|終了)', joined)
    if negative:
        return '廃止・未実施等の記載あり（要確認）'
    return '訂正記載あり（要確認）' if correction else '優待記載あり（要確認）'

def make_items(doc, master, passages, checked, correction=False):
    code = str(doc.get('secCode') or '')[:4]
    status = classify(passages, correction)
    if not passages or status.startswith('廃止'):
        return []
    joined = '\n\n'.join(p['text'] for p in passages)
    # Accept months only from explicit entitlement/record-date wording. Do not
    # mistake "6か月継続" or fiscal period dates elsewhere for entitlement months.
    dates = []
    for line in joined.splitlines():
        if not re.search(r'基準日|権利確定|株主名簿|月末(?:日)?現在', line):
            continue
        for m in re.finditer(r'(?<!\d)(1[0-2]|[1-9])月\s*(末日?|\d{1,2}日)', line):
            token = m.group(0)
            pair = (int(m.group(1)), token)
            if pair not in dates:
                dates.append(pair)
    months = sorted({m for m, _ in dates}) or [None]
    conditions = []
    for line in joined.splitlines():
        if re.search(r'\d[\d,]*\s*株|継続保有|継続して保有|保有期間', line):
            conditions.append(line[:1000])
    condition = ' / '.join(dict.fromkeys(conditions))[:4000] or '要確認'
    source = 'EDINET提出本文・自動抽出（要確認）; docID=' + str(doc.get('docID'))
    return [dict(code=code, name=doc.get('filerName') or master.get('filerName') or '',
                 industry=master.get('industry') or '要確認', month=m,
                 recordDate='・'.join(t for mo, t in dates if mo == m) or '要確認',
                 conditions=condition, benefit=joined, officialSource='',
                 confirmedAt=checked, status=status, sourceType=source) for m in months]

class Collector:
    def __init__(self, bridge):
        self.bridge = bridge
        self.root = os.environ.get('EDINET_BENEFITS_DIR', os.path.join(bridge.EDINET_CACHE_DIR, 'benefits'))
        self.lock = threading.RLock()
        self.worker = None
        self.stop = threading.Event()
        self.state = self._load()
        if self.state and self.state.get('status') == 'running':
            self.state['status'] = 'interrupted'
            self.state['message'] = 'サーバーが再起動しました。「再開」で保存済み進捗から続行できます。'
    def _load(self):
        try:
            with open(os.path.join(self.root, 'state.json'), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    def _save(self):
        os.makedirs(self.root, exist_ok=True)
        p = os.path.join(self.root, 'state.json')
        with open(p + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(p + '.tmp', p)
    def view(self):
        with self.lock:
            if not self.state:
                return {'status': 'idle', 'keyConfigured': bool(self.bridge.EDINET_API_KEY)}
            s = {k: copy.deepcopy(v) for k, v in self.state.items()
                 if k not in ('documents', 'processed', 'results', 'master', 'skippedDocuments')}
            s['keyConfigured'] = bool(self.bridge.EDINET_API_KEY)
            s['companyCount'] = len({x['code'] for x in self.state.get('results', [])})
            s['itemCount'] = len(self.state.get('results', []))
            s['reportCount'] = len(self.state.get('documents', {}))
            s['processedReports'] = len(self.state.get('processed', {}))
            return s
    def start(self, payload):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError('取得は実行中です')
            if not self.bridge.EDINET_API_KEY:
                raise ValueError('RenderのEDINET_API_KEYが設定されていません')
            action = payload.get('action', 'start')
            if action == 'resume':
                if not self.state or self.state.get('status') == 'completed':
                    raise ValueError('再開できる処理がありません')
                # Retry failed lists and document downloads; successes are retained.
                self.state['daysDone'] = [d for d in self.state['daysDone'] if d not in self.state.get('failedDays', [])]
                self.state['failedDays'] = []
                for doc_id in self.state.get('failedDocuments', []):
                    self.state['processed'].pop(doc_id, None)
                self.state['failedDocuments'] = []
                self.state['errors'] = []
            elif action == 'start':
                end = date.fromisoformat(payload.get('endDate') or datetime.now(JST).date().isoformat())
                begin = date.fromisoformat(payload.get('startDate') or (end - timedelta(days=399)).isoformat())
                if not 1 <= (end - begin).days + 1 <= 550 or end > datetime.now(JST).date():
                    raise ValueError('期間は過去の日付で1〜550日以内にしてください')
                self.state = dict(status='running', startDate=begin.isoformat(), endDate=end.isoformat(),
                                  phase='lists', daysDone=[], failedDays=[], failedDocuments=[],
                                  documents={}, processed={}, results=[], errors=[], checkedAt=None,
                                  master={}, corrections=[], skippedDocuments={}, skippedReports=0, message='書類一覧を取得しています')
            else:
                raise ValueError('actionはstartまたはresumeを指定してください')
            self.state['status'] = 'running'
            self.stop.clear()
            self._save()
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()
            return self.view()
    def pause(self):
        self.stop.set()
        return {'message': '現在のリクエスト完了後に中断します'}
    def _failure(self, stage, key, exc):
        # Never return/log exception URLs: they may contain Subscription-Key.
        with self.lock:
            self.state['errors'].append({'stage': stage, 'id': key,
                                         'type': type(exc).__name__, 'httpStatus': getattr(exc, 'code', None)})
            self.state['errors'] = self.state['errors'][-200:]
    def _call(self, fn):
        for attempt in range(3):
            try:
                result = fn()
                time.sleep(.35)
                return result
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise
                if exc.code != 429 and exc.code < 500:
                    raise
                if attempt == 2:
                    raise
            except RuntimeError as exc:
                if re.search(r'401|403|invalid subscription|access denied|not configured', str(exc), re.I):
                    raise PermissionError('EDINET認証エラー') from None
                raise
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise
            if self.stop.wait(2 ** (attempt + 1)):
                raise InterruptedError('paused')
    def _daily(self, day):
        # Fresh official list scan (existing financial cache has a 365-day TTL,
        # which can hide later withdrawals and corrections). Cache only this job.
        data = self.bridge._edinet_get_json('https://api.edinet-fsa.go.jp/api/v2/documents.json?date=%s&type=2' % day)
        return data.get('results') or []
    def _download(self, doc_id):
        url = 'https://api.edinet-fsa.go.jp/api/v2/documents/%s?type=1&Subscription-Key=%s' % (
            quote(doc_id, safe=''), quote(self.bridge.EDINET_API_KEY, safe=''))
        with urlopen(Request(url, headers={'User-Agent': self.bridge.USER_AGENT}), timeout=45) as r:
            body = r.read(MAX_ZIP + 1)
        if len(body) > MAX_ZIP:
            raise ValueError('書類ZIPのサイズ上限を超えました')
        if not body.startswith(b'PK'):
            try:
                error = json.loads(body.decode('utf-8'))
                if str(error.get('StatusCode')) in ('401', '403'):
                    raise PermissionError('EDINET認証エラー')
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise ValueError('EDINETがZIP以外の応答を返しました。APIキー・制限をご確認ください')
        return body
    def _run(self):
        try:
            with self.lock:
                begin, end = date.fromisoformat(self.state['startDate']), date.fromisoformat(self.state['endDate'])
            if not self.state['master']:
                try:
                    master = self._call(self.bridge._edinet_load_codelist)
                    with self.lock:
                        self.state['master'] = master
                except Exception as exc:
                    self._failure('industryMaster', '', exc)
            day = end
            while day >= begin and not self.stop.is_set():
                ds = day.isoformat()
                if ds not in self.state['daysDone']:
                    try:
                        rows = self._call(lambda: self._daily(ds))
                        with self.lock:
                            for row in rows:
                                sec = str(row.get('secCode') or '')
                                if not re.fullmatch(r'(?:\d{4}|\d{3}[A-Z])0', sec):
                                    continue
                                if str(row.get('docTypeCode')) not in ('120', '130', '160', '170', '180', '190'):
                                    continue
                                if str(row.get('withdrawalStatus') or '0') == '2' or str(row.get('disclosureStatus') or '0') == '2':
                                    continue
                                if str(row.get('xbrlFlag') or '0') != '1':
                                    self.state.setdefault('skippedDocuments', {})[str(row.get('docID'))] = {'docID': row.get('docID'), 'name': row.get('filerName'), 'reason': '提出本文HTML/XBRL対象外'}
                                    self.state['skippedReports'] = len(self.state['skippedDocuments'])
                                    continue
                                if re.fullmatch(r'[A-Z0-9]{8}', str(row.get('docID') or '')):
                                    self.state['documents'][row['docID']] = row
                    except InterruptedError:
                        break
                    except Exception as exc:
                        self._failure('list', ds, exc)
                        if isinstance(exc, PermissionError) or (isinstance(exc, HTTPError) and exc.code in (401, 403)):
                            raise
                        with self.lock:
                            self.state['failedDays'].append(ds)
                    with self.lock:
                        self.state['daysDone'].append(ds)
                        self.state['currentDate'] = ds
                        self._save()
                day -= timedelta(days=1)
            if not self.stop.is_set():
                with self.lock:
                    self.state['phase'] = 'documents'
                    self.state['message'] = '提出本文から株主優待の記載を抽出しています'
                # Keep all interim/extraordinary/correction reports; annual reports
                # use the newest annual per company in this date window.
                docs = list(self.state['documents'].values())
                annual = {}
                for d in docs:
                    if str(d.get('docTypeCode')) == '120':
                        code = str(d['secCode'])[:4]
                        if code not in annual or (d.get('submitDateTime') or '') > (annual[code].get('submitDateTime') or ''):
                            annual[code] = d
                selected = list(annual.values()) + [d for d in docs if str(d.get('docTypeCode')) != '120']
                selected.sort(key=lambda d: (d.get('submitDateTime') or '', d['docID']))
                with self.lock:
                    self.state['selectedReports'] = len(selected)
                for d in selected:
                    if self.stop.is_set():
                        break
                    did = d['docID']
                    if did in self.state['processed']:
                        continue
                    try:
                        passages = extract_zip(self._call(lambda: self._download(did)))
                        checked = datetime.now(JST).date().isoformat()
                        corr = str(d.get('docTypeCode')) in ('130', '170', '190')
                        result = {'docID': did, 'code': str(d['secCode'])[:4],
                                  'name': d.get('filerName'), 'submitDateTime': d.get('submitDateTime'),
                                  'docDescription': d.get('docDescription'), 'parentDocID': d.get('parentDocID'),
                                  'classification': classify(passages, corr) if passages else '優待記載未検出（不存在の証明ではありません）',
                                  'passages': passages}
                        with self.lock:
                            self.state['processed'][did] = result
                            self.state['checkedAt'] = checked
                    except InterruptedError:
                        break
                    except Exception as exc:
                        self._failure('document', did, exc)
                        if isinstance(exc, PermissionError) or (isinstance(exc, HTTPError) and exc.code in (401, 403)):
                            raise
                        with self.lock:
                            self.state['failedDocuments'].append(did)
                            self.state['processed'][did] = {'docID': did, 'classification': '取得・解析失敗', 'passages': []}
                    with self.lock:
                        self._rebuild()
                        self.state['currentDocument'] = did
                        self._save()
            with self.lock:
                self._rebuild()
                self.state['status'] = ('paused' if self.stop.is_set() else
                                        'partial' if self.state['failedDays'] or self.state['failedDocuments'] else 'completed')
                self.state['message'] = {'paused': '中断しました。保存済み進捗から再開できます。',
                                         'partial': '一部の取得に失敗しました。再開すると失敗分を再試行します。',
                                         'completed': '指定期間の取得を完了しました。抽出候補と確認用記録をダウンロードできます。'}[self.state['status']]
                self._save()
        except Exception as exc:
            self._failure('job', '', exc)
            with self.lock:
                self.state['status'] = 'failed'
                self.state['message'] = '取得を停止しました。RenderのAPIキー・接続・保存領域を確認して再開してください。'
                try:
                    self._save()
                except OSError:
                    pass
    def _rebuild(self):
        # A newer mention takes precedence. An absence of keyword is not treated
        # as abolition. Negative mentions remove older positive candidates.
        company = {}
        docs = sorted(self.state['processed'].values(), key=lambda d: (d.get('submitDateTime') or '', d['docID']))
        for p in docs:
            if not p.get('passages'):
                continue
            d = self.state['documents'][p['docID']]
            code = str(d['secCode'])[:4]
            company[code] = make_items(d, self.state['master'].get(code, {}), p['passages'],
                                       self.state['checkedAt'], str(d.get('docTypeCode')) in ('130', '170', '190'))
        self.state['results'] = sorted([x for rows in company.values() for x in rows], key=lambda x: (x['month'] or 99, x['code']))
    def export(self):
        with self.lock:
            s = self.state
            if not s:
                raise ValueError('まだ取得結果がありません')
            return {'updatedAt': s.get('checkedAt') or datetime.now(JST).date().isoformat(),
                    'dataStatus': 'EDINET API v2提出本文から優待記載を自動抽出。期間%s〜%s、処理状態%s。最新制度・全優待銘柄の網羅性は未確認。' % (s['startDate'], s['endDate'], s['status']),
                    'notice': '優待の記載がある候補データです。month=nullは権利月未判定。confirmedAtは本文抽出日であり制度の最新確認日ではありません。officialSourceは企業IR未照合のため空欄。訂正本文は部分的なため原本との照合が必要です。根拠のdocIDはsourceType、原文抜粋・廃止等・未検出・失敗記録は確認用JSONに収録。',
                    'items': copy.deepcopy(s['results'])}
    def evidence(self):
        with self.lock:
            if not self.state:
                raise ValueError('まだ取得結果がありません')
            return {'progress': self.view(), 'documents': copy.deepcopy(list(self.state['processed'].values())),
                    'skippedDocuments': copy.deepcopy(list(self.state.get('skippedDocuments', {}).values()))}

_instance = None
_instance_lock = threading.Lock()
def collector(bridge):
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Collector(bridge)
        return _instance

def handle_get(handler, path, bridge):
    if path == '/edinet-benefits':
        with open(os.path.join(os.path.dirname(__file__), 'edinet_benefits.html'), 'rb') as f:
            body = f.read()
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path not in ('/api/edinet/benefits/status', '/api/edinet/benefits/export', '/api/edinet/benefits/evidence'):
        return False
    c = collector(bridge)
    try:
        result = c.view() if path.endswith('/status') else c.evidence() if path.endswith('/evidence') else c.export()
        handler.send_json(200, result)
    except ValueError as exc:
        handler.send_json(404, {'error': str(exc)})
    return True

def handle_post(handler, path, bridge):
    if path != '/api/edinet/benefits/job':
        return False
    token = os.environ.get('EDINET_BENEFITS_ADMIN_TOKEN', '').strip()
    if token:
        auth = handler.headers.get('Authorization', '')
        if not secrets.compare_digest(auth, 'Bearer ' + token):
            handler.send_json(401, {'error': '取得用管理トークンを確認してください'})
            return True
    elif handler.headers.get('Origin') not in bridge.ALLOWED_ORIGINS:
        handler.send_json(403, {'error': '許可されていない送信元です'})
        return True
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 1 <= length <= 2048:
            raise ValueError('送信サイズが不正です')
        payload = json.loads(handler.rfile.read(length).decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError('JSONオブジェクトを指定してください')
        c = collector(bridge)
        result = c.pause() if payload.get('action') == 'pause' else c.start(payload)
        handler.send_json(202, result)
    except (ValueError, UnicodeDecodeError) as exc:
        handler.send_json(400, {'error': str(exc)})
    except OSError:
        handler.send_json(503, {'error': '進捗の保存先に書き込めません'})
    return True
