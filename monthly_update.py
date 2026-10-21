"""Monthly EDINET update. Run in GitHub Actions with EDINET_API_KEY secret."""
import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

def run(today, force=False):
    import calendar
    import server
    import edinet_benefits
    if not force and today.day != calendar.monthrange(today.year, today.month)[1]:
        print('Not the final day of month in JST: skip')
        return
    if not os.environ.get('EDINET_API_KEY'):
        raise RuntimeError('EDINET_API_KEY GitHub Actions secret is required')
    begin = today.replace(day=1)
    with tempfile.TemporaryDirectory() as work:
        os.environ['EDINET_BENEFITS_DIR'] = work
        c = edinet_benefits.Collector(server)
        c.start({'action': 'start', 'startDate': begin.isoformat(), 'endDate': today.isoformat()})
        c.worker.join(timeout=5 * 60 * 60)
        if c.worker.is_alive():
            c.pause()
            raise TimeoutError('EDINET job exceeded 5 hours')
        status = c.view()
        if status['status'] != 'completed':
            raise RuntimeError('EDINET collection incomplete: status=%s, failedDays=%s, failedDocuments=%s' % (
                status['status'], len(status.get('failedDays', [])), len(status.get('failedDocuments', []))))
        export = c.export()
        evidence = c.evidence()
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
