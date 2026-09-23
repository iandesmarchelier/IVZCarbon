"""Inventory persistence.

The catalogue (factors, sites, periods, settings...) is small and stays one JSON body in
carbon_states. Records (REC) and material movements (MOV) are rows, so the screen loads them in
pages and saves only what changed: no request has to carry the whole inventory, which Vercel
caps at 4.5 MB. Every save still validates and computes the complete inventory with
normalize() and compute(), so results are exactly those of the single-body format.
"""
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from fastapi import HTTPException
from .metrics import normalize, compute
from .storage import db, decode, event

ROWS = {'REC': 'rid', 'MOV': 'id'}
CATALOGUE_KINDS = ('FACTORS', 'SITES', 'PROCS', 'LINES', 'MACH', 'BIZ', 'WASTECAT', 'RULES')
PAGE_MAX = 5000
UPLOAD_TTL = 86400
CONFLICT = 'Otra pestaña guardó cambios. Descargá tus cambios y recargá antes de continuar.'


def _select(s, user, lock):
    sql = 'SELECT revision,body,updated FROM carbon_states WHERE account=?'
    return s.execute(sql + (' FOR UPDATE' if lock and s.postgres else ''), (user,)).fetchone()


def _catalogue(s, user, lock=False):
    """Return (row, catalogue body), moving a pre-split inventory into rows the first time it is read."""
    row = _select(s, user, lock)
    if not row:
        return None, None
    body = decode(row['body'])
    if 'REC' in body or 'MOV' in body:
        if not lock:
            return _catalogue(s, user, True)
        s.execute('INSERT INTO carbon_state_backups (account,created,revision,body) VALUES (?,?,?,?)',
                  (user, datetime.now(timezone.utc).isoformat(), row['revision'], s.json(body)))
        for kind in ROWS:
            s.execute(*_delete_all(user, kind))
            _insert(s, user, kind, list(enumerate(body.pop(kind, []))))
        s.execute('UPDATE carbon_states SET body=? WHERE account=?', (s.json(body), user))
    return row, body


def _delete_all(user, kind):
    if kind == 'REC':
        return 'DELETE FROM carbon_records WHERE account=?', (user,)
    return "DELETE FROM carbon_entities WHERE account=? AND kind='MOV'", (user,)


def _rows(s, user, kind, offset=0, limit=None):
    """[(seq, item)] in inventory order."""
    if kind == 'REC':
        sql = 'SELECT seq,body FROM carbon_records WHERE account=? ORDER BY seq,id'
    else:
        sql = "SELECT seq,body FROM carbon_entities WHERE account=? AND kind='MOV' ORDER BY seq,id"
    args = [user]
    if limit is not None:
        sql += ' LIMIT ? OFFSET ?'
        args += [limit, offset]
    return [(r['seq'], decode(r['body'])) for r in s.execute(sql, tuple(args)).fetchall()]


def _insert(s, user, kind, rows):
    if kind == 'REC':
        s.executemany('INSERT INTO carbon_records (account,id,period,site,scope,factor,quantity,kg,body,seq) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      [(user, r['rid'], r['p'], r['site'], r['scope'], r['factor'], r['qty'], r['kg'], s.json(r), seq) for seq, r in rows])
    else:
        s.executemany("INSERT INTO carbon_entities (account,kind,id,body,seq) VALUES (?,'MOV',?,?,?)",
                      [(user, m['id'], s.json(m), seq) for seq, m in rows])


def _delete(s, user, kind, ids):
    if kind == 'REC':
        s.executemany('DELETE FROM carbon_records WHERE account=? AND id=?', [(user, i) for i in ids])
    else:
        s.executemany("DELETE FROM carbon_entities WHERE account=? AND kind='MOV' AND id=?", [(user, i) for i in ids])


def load(user):
    """The complete inventory, as the single-body format returned it."""
    with db() as s:
        row, body = _catalogue(s, user)
        if not row:
            return {'revision': 0, 'state': None, 'updated': None}
        state = dict(body, **{kind: [item for _, item in _rows(s, user, kind)] for kind in ROWS})
    return {'revision': row['revision'], 'state': state, 'updated': row['updated']}


@contextmanager
def snapshot(user):
    """One consistent view for long downloads: ({'revision', 'updated', 'state': catalogue} or None, rows(kind)).

    rows(kind) yields the items in order, reading them from the database in batches instead of all at once."""
    with db() as s:
        if s.postgres:
            s.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        row, body = _catalogue(s, user)

        def rows(kind):
            table = 'carbon_records WHERE account=?' if kind == 'REC' else "carbon_entities WHERE account=? AND kind='MOV'"
            cursor = s.execute(f'SELECT body FROM {table} ORDER BY seq,id', (user,))
            while batch := cursor.fetchmany(1000):
                for r in batch:
                    yield decode(r['body'])
        yield (row and {'revision': row['revision'], 'updated': row['updated'], 'state': body}), rows


def load_catalogue(user):
    with db() as s:
        row, body = _catalogue(s, user)
        if not row:
            return {'revision': 0, 'state': None, 'updated': None, 'counts': {}}
        counts = {'REC': s.execute('SELECT COUNT(*) AS n FROM carbon_records WHERE account=?', (user,)).fetchone()['n'],
                  'MOV': s.execute("SELECT COUNT(*) AS n FROM carbon_entities WHERE account=? AND kind='MOV'", (user,)).fetchone()['n']}
    return {'revision': row['revision'], 'state': body, 'updated': row['updated'], 'counts': counts}


def load_page(user, kind, offset, limit):
    if kind not in ROWS:
        raise HTTPException(422, 'Tipo de dato desconocido.')
    with db() as s:
        row, _ = _catalogue(s, user)
        if not row:
            raise HTTPException(404, 'Inicializá el inventario.')
        items = [item for _, item in _rows(s, user, kind, max(0, offset), max(1, min(limit, PAGE_MAX)))]
    return {'revision': row['revision'], 'items': items}


def upload(user, batch, part, changes):
    """Stage part of a change set too large for one request; the save that names the batch applies it."""
    _check_changes(changes, staged=True)
    with db() as s:
        s.execute('DELETE FROM carbon_uploads WHERE created<?', (time.time() - UPLOAD_TTL,))
        s.execute('DELETE FROM carbon_uploads WHERE account=? AND batch=? AND part=?', (user, batch, part))
        s.execute('INSERT INTO carbon_uploads (account,batch,part,created,body) VALUES (?,?,?,?,?)',
                  (user, batch, part, time.time(), s.json(changes)))
    return {'ok': True}


def _check_changes(changes, staged=False):
    if not isinstance(changes, dict) or set(changes) - set(ROWS):
        raise HTTPException(422, 'Cambios inválidos.')
    for kind, change in changes.items():
        if not isinstance(change, dict) or set(change) - ({'upsert'} if staged else {'upsert', 'delete'}):
            raise HTTPException(422, 'Cambios inválidos.')
        if not isinstance(change.get('upsert', []), list) or not isinstance(change.get('delete', []), list):
            raise HTTPException(422, 'Cambios inválidos.')
        if any(not isinstance(x, dict) or not isinstance(x.get(ROWS[kind]), str) for x in change.get('upsert', [])):
            raise HTTPException(422, 'Registro sin identificador.')
        if any(not isinstance(x, str) for x in change.get('delete', [])):
            raise HTTPException(422, 'Identificador inválido.')


def _merge(old, key, change, order):
    """Apply upserts (replace in place, new ones at the end) and deletions; then an explicit order if given."""
    new = {}
    for item in change.get('upsert', []):
        new[item[key]] = item
    deleted = set(change.get('delete', [])) - set(new)
    result = [new.pop(item[key], item) for _, item in old if item[key] not in deleted]
    result += new.values()
    if order is not None:
        ids = [item[key] for item in result]
        if not isinstance(order, list) or len(order) != len(ids) or set(order) != set(ids):
            raise ValueError('Orden de registros inválido.')
        position = {k: i for i, k in enumerate(order)}
        result.sort(key=lambda item: position[item[key]])
    return result


def _sync(s, user, kind, old, new):
    """Write only the rows whose content or position changed; returns how many were written or removed."""
    key = ROWS[kind]
    before = {item[key]: (seq, item) for seq, item in old}
    survivors = [before[item[key]][0] for item in new if item[key] in before]
    in_order = all(a is not None and b is not None and a < b for a, b in zip(survivors, survivors[1:])) and \
        all(item[key] in before for item in new[:len(survivors)])
    if in_order:
        top = max([seq for seq, _ in old if seq is not None], default=-1)
        seqs, tail = [], 0
        for item in new:
            if item[key] in before:
                seqs.append(before[item[key]][0])
            else:
                tail += 1
                seqs.append(top + tail)
    else:
        seqs = list(range(len(new)))
    keep = {item[key] for item in new}
    removed = [k for k in before if k not in keep]
    changed = [(seq, item) for seq, item in zip(seqs, new) if before.get(item[key]) != (seq, item)]
    _delete(s, user, kind, removed + [item[key] for _, item in changed if item[key] in before])
    _insert(s, user, kind, changed)
    return len(changed) + len(removed)


def save(user, revision, action='save', full=None, catalogue=None, changes=None, order=None, batch=None, parts=0):
    """Save a complete inventory (full) or a change set, validated as a whole. Returns revision and summary."""
    changes = changes or {}
    order = order or {}
    if full is None:
        _check_changes(changes)
        if not isinstance(order, dict) or set(order) - set(ROWS):
            raise HTTPException(422, 'Orden de registros inválido.')
    updated = datetime.now(timezone.utc).isoformat()
    with db() as s:
        row, body = _catalogue(s, user, lock=True)
        current = row['revision'] if row else 0
        if revision != current:
            raise HTTPException(409, CONFLICT)
        old = {kind: _rows(s, user, kind) for kind in ROWS}
        if batch:
            staged = s.execute('SELECT body FROM carbon_uploads WHERE account=? AND batch=? ORDER BY part', (user, batch)).fetchall()
            s.execute('DELETE FROM carbon_uploads WHERE account=? AND batch=?', (user, batch))
            if len(staged) != parts:
                raise HTTPException(409, 'El guardado quedó incompleto. Reintentá.')
            for kind in ROWS:
                upserts = [x for r in staged for x in decode(r['body']).get(kind, {}).get('upsert', [])]
                if upserts:
                    change = changes.setdefault(kind, {})
                    change['upsert'] = upserts + change.get('upsert', [])
        try:
            if full is not None:
                raw = full
            else:
                if catalogue is None and body is None:
                    raise ValueError('Inicializá el inventario.')
                raw = dict(catalogue if catalogue is not None else body)
                for kind, key in ROWS.items():
                    raw[kind] = _merge(old[kind], key, changes.get(kind, {}), order.get(kind))
            state = normalize(raw)
            summary = compute(state)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        new_body = {k: v for k, v in state.items() if k not in ROWS}
        if row:
            s.execute('UPDATE carbon_states SET revision=revision+1,body=?,updated=? WHERE account=?', (s.json(new_body), updated, user))
        elif not s.execute('INSERT INTO carbon_states VALUES (?,1,?,?) ON CONFLICT(account) DO NOTHING RETURNING revision',
                           (user, s.json(new_body), updated)).fetchone():
            raise HTTPException(409, CONFLICT)
        if body is None or any(body.get(kind) != new_body.get(kind) for kind in CATALOGUE_KINDS):
            # Queryable copy of the catalogue, as before the split.
            s.execute('DELETE FROM carbon_entities WHERE account=? AND kind<>?', (user, 'MOV'))
            s.executemany('INSERT INTO carbon_entities (account,kind,id,body,seq) VALUES (?,?,?,?,?)',
                          [(user, kind, item['id'], s.json(item), i) for kind in CATALOGUE_KINDS for i, item in enumerate(new_body[kind])])
        written = {kind: _sync(s, user, kind, old[kind], state[kind]) for kind in ROWS}
        event(s, user, action, current + 1, {'records': len(state['REC']), 'kg': summary['kg'], 'written': written})
    return {'revision': current + 1, 'updated': updated, 'summary': summary}
