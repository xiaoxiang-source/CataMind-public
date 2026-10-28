"""PDOS preview -> scientific acceptance -> explicit, idempotent ML admission."""
import hashlib
import json
import math
import secrets
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdos_reviews (
    id TEXT PRIMARY KEY,
    uuid TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    descriptors TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview',
    reviewer TEXT,
    review_note TEXT,
    checks TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    accepted_at TEXT,
    admitted_at TEXT,
    sample_id INTEGER UNIQUE,
    UNIQUE(uuid, fingerprint)
);
"""
CHECKS = ('convergence', 'projection', 'units_window', 'comparability')


def snapshot(directory):
    return [{'file': str(p.resolve()), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(Path(directory).glob('*.pdos'))]


def response(row):
    return {'review_id': row['id'], 'uuid': row['uuid'], 'status': row['status'],
            'sample_id': row['sample_id'], 'descriptors': json.loads(row['descriptors']),
            'review': {'reviewer': row['reviewer'], 'note': row['review_note'],
                       'accepted_at': row['accepted_at'], 'admitted_at': row['admitted_at'],
                       'checks': json.loads(row['checks'] or '{}')}}


def create_preview(conn, uuid, result, before, after):
    sources = result['provenance']['sources']
    parsed = [{'file': s['file'], 'sha256': s['sha256']} for s in sources]
    if before != after or parsed != after:
        raise ValueError('解析过程中 PDOS 文件发生变化，请计算结束后重新预览')
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    row = conn.execute('SELECT * FROM pdos_reviews WHERE uuid=? AND fingerprint=?', (uuid, fingerprint)).fetchone()
    if row is None:
        rid = secrets.token_hex(16)
        conn.execute('INSERT INTO pdos_reviews(id,uuid,fingerprint,descriptors) VALUES(?,?,?,?)', (rid, uuid, fingerprint, encoded))
        row = conn.execute('SELECT * FROM pdos_reviews WHERE id=?', (rid,)).fetchone()
    return response(row)


def transition(conn, rid, action, body, reviewer, directory):
    row = conn.execute('SELECT * FROM pdos_reviews WHERE id=?', (rid,)).fetchone()
    if row is None:
        raise ValueError('预览记录不存在，请重新解析')
    result = json.loads(row['descriptors'])
    expected = [{'file': s['file'], 'sha256': s['sha256']} for s in result['provenance']['sources']]
    if snapshot(directory) != expected:
        raise ValueError('源 PDOS 已变化，旧预览不能验收或入库，请重新解析')
    if action == 'accept':
        if row['status'] == 'preview':
            checks = body.get('checks') or {}
            note = str(body.get('note') or '').strip()
            if any(checks.get(k) is not True for k in CHECKS) or not note:
                raise ValueError('须逐项确认收敛、投影、单位/能窗、可比性，并填写科学验收说明')
            conn.execute("UPDATE pdos_reviews SET status='accepted',reviewer=?,review_note=?,checks=?,accepted_at=datetime('now','localtime') WHERE id=?",
                         (reviewer, note, json.dumps({k: True for k in CHECKS}), rid))
    elif action == 'admit':
        if row['status'] not in ('accepted', 'admitted'):
            raise ValueError('须先完成科学验收，再纳入训练集')
        if row['sample_id'] is None:
            features = {}
            for element, metrics in result['elements'].items():
                for field in ('d_band_center', 'p_band_center', 'd_ldos_fermi', 'p_ldos_fermi'):
                    value = metrics.get(field)
                    if value is not None and math.isfinite(value):
                        features[field + '_' + element] = value
            for field in ('d_band_center', 'total_dos_fermi'):
                value = result['summary'].get(field)
                if value is not None and math.isfinite(value):
                    features[field] = value
            cur = conn.execute('INSERT INTO ml_samples(name,source,features,target) VALUES(?,?,?,NULL)',
                               (row['uuid'][:8], 'pdos_review:' + rid, json.dumps(features, allow_nan=False)))
            conn.execute("UPDATE pdos_reviews SET status='admitted',sample_id=?,admitted_at=datetime('now','localtime') WHERE id=?", (cur.lastrowid, rid))
    else:
        raise ValueError('未知 PDOS 操作')
    return response(conn.execute('SELECT * FROM pdos_reviews WHERE id=?', (rid,)).fetchone())


def provenance_by_source(conn):
    mapping = {}
    for row in conn.execute("SELECT * FROM pdos_reviews WHERE sample_id IS NOT NULL"):
        data = response(row)
        mapping['pdos_review:' + row['id']] = {
            'status': data['status'], 'review_id': row['id'], 'uuid': row['uuid'],
            'provenance': data['descriptors']['provenance'], 'review': data['review']}
    mapping['postprocess'] = {'status': 'legacy_unreviewed', 'note': '历史自动入库记录，未经过科学验收；解析版本和单位待核查'}
    return mapping
