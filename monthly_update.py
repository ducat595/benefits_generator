"""Monthly EDINET update. GitHub Actions calls the existing Render service."""
import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import time

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
DATA = BASE / 'shareholder_benefits.json'
LOG = BASE / 'benefits_update_history.json'

def load(path, fallback):
    if not path.exists():
        return fallback
    with path.open(encoding='utf-8') as f:
        return json.load(f)

def save(path, obj):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.monthly_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def key(item):
    return (str(item.get('code', '')), str(item.get('month') or ''))

def merge(old, incoming, evidence):
    """Never delete missing companies. Keep uncertain corrections/abolitions for review."""
    merged = {key(x): x for x in old}
    changes, reviews = [], []
    for item in incoming:
        k = key(item)
        if not k[0]:
            continue
        status = item.get('status', '')
        if '訂正' in status or '廃止' in status or '未実施' in status:
            reviews.append({'code': k[0], 'month': item.get('month'), 'reason': status, 'sourceType': item.get('sourceType')})
            continue
        before = merged.get(k)
        if before is None:
            merged[k] = item
            changes.append({'code': k[0], 'month': item.get('month'), 'type': 'added'})
        else:
            fields = ('name', 'industry', 'month', 'recordDate', 'conditions', 'benefit', 'status')
            changed = [field for field in fields if before.get(field) != item.get(field)]
            if changed:
                reviews.append({'code': k[0], 'month': item.get('month'), 'reason': 'existing record differs',
                                'fields': changed, 'proposed': item, 'previous': before})
    for doc in evidence.get('documents', []):
        if '廃止' in doc.get('classification', '') or '訂正' in doc.get('classification', ''):
            reviews.append({'code': doc.get('code'), 'docID': doc.get('docID'), 'reason': doc.get('classification')})
    return sorted(merged.values(), key=lambda x: (str(x.get('code', '')), str(x.get('month') or ''))), changes, reviews

def render_request(base, path, token=None, payload=None):
    """Call the Render app without exposing the EDINET API key."""
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'Accept': 'application/json', 'User-Agent': 'BenefitsMonthlyUpdater/1.0'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
        headers['Authorization'] = 'Bearer ' + token
    req = Request(base + path, data=body, headers=headers, method='POST' if body is not None else 'GET')
    try:
        with urlopen(req, timeout=90) as response:
            result = json.loads(response.read(40 * 1024 * 1024 + 1))
    except HTTPError as exc:
        raise RuntimeError('Render HTTP %s at %s' % (exc.code, path)) from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError('Render communication failure at %s (%s)' % (path, type(exc).__name__)) from None
    if not isinstance(result, dict):
        raise RuntimeError('Unexpected Render JSON response at ' + path)
    if 'error' in result:
        raise RuntimeError('Render error at %s: %s' % (path, result['error']))
    return result


def run(today, force=False):
    import calendar
    if not force and today.day != calendar.monthrange(today.year, today.month)[1]:
        print('Not the final day of month in JST: skip')
        return
    base = os.environ.get('EDINET_RENDER_URL', 'https://edinet-benefits-generator.onrender.com').rstrip('/')
    if base != 'https://edinet-benefits-generator.onrender.com':
        raise ValueError('Unexpected Render host')
    token = os.environ.get('EDINET_BENEFITS_ADMIN_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Set EDINET_BENEFITS_ADMIN_TOKEN in BOTH Render and GitHub Actions Secrets')
    begin = today.replace(day=1)
    expected_start, expected_end = begin.isoformat(), today.isoformat()
    # The Render worker persists its job in /tmp; an unexpected restart means failure,
    # not a successful empty scan. Never change the master for partial/failed results.
    started = render_request(base, '/api/edinet/benefits/job', token, {
        'action': 'start', 'startDate': expected_start, 'endDate': expected_end})
    if started.get('status') != 'running':
        raise RuntimeError('Render job did not start')
    deadline = time.monotonic() + 5 * 60 * 60
    while time.monotonic() < deadline:
        time.sleep(20)
        status = render_request(base, '/api/edinet/benefits/status')
        if status.get('startDate') != expected_start or status.get('endDate') != expected_end:
            raise RuntimeError('Render job disappeared/restarted; refusing to update master')
        state = status.get('status')
        if state in ('completed', 'partial', 'failed', 'interrupted', 'paused'):
            break
    else:
        raise TimeoutError('Render job exceeded 5 hours')
    if state != 'completed' or status.get('failedDays') or status.get('failedDocuments'):
        raise RuntimeError('EDINET collection incomplete: status=%s' % state)
    export = render_request(base, '/api/edinet/benefits/export')
    if expected_start not in export.get('dataStatus', '') or expected_end not in export.get('dataStatus', ''):
        raise RuntimeError('Export does not match requested period; refusing to overwrite data')
    evidence = render_request(base, '/api/edinet/benefits/evidence')
    if not isinstance(export.get('items'), list):
        raise RuntimeError('Export items must be an array')
    if not DATA.exists():
        raise FileNotFoundError('既存のshareholder_benefits.jsonを配置してください。過去データ保護のため初期化しません。')
    existing = load(DATA, {})
    if not isinstance(existing.get('items'), list):
        raise ValueError('既存JSONのitems配列が不正です')
    combined, changes, reviews = merge(existing.get('items', []), export['items'], evidence)
    if changes:
        existing['items'] = combined
        existing['updatedAt'] = today.isoformat()
        existing['dataStatus'] = 'EDINET候補の追加を反映。既存銘柄の変更は要確認として保留。'
        save(DATA, existing)
    history = load(LOG, {'runs': []})
    history['runs'].append({'month': today.strftime('%Y-%m'), 'checkedAt': datetime.now(JST).isoformat(),
                            'startDate': begin.isoformat(), 'endDate': today.isoformat(),
                            'newRecords': len(changes), 'reviewCount': len(reviews),
                            'candidateCount': len(export['items']), 'status': 'completed', 'reviews': reviews})
    history['runs'] = history['runs'][-24:]
    save(LOG, history)
    print('EDINET monthly completed: new=%s review=%s' % (len(changes), len(reviews)))

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--force', action='store_true', help='run even before month-end')
    args = p.parse_args()
    run(datetime.now(JST).date(), force=args.force)
