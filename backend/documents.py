"""Uploaded bills and manifests, kept so a record can open the document it came from.

A file is stored when it is read (/api/parse-document) and becomes linked once a saved record
names it in origin.files. Files never linked (the upload was discarded) are removed after a day.
"""
import time
import uuid
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

from .storage import db

ORPHAN_TTL = 86400
# The type comes from the file's first bytes, never from what the browser says.
SIGNATURES = [(b'%PDF', 'application/pdf'), (b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg')]


def kind_of(data):
    return next((media for magic, media in SIGNATURES if data.startswith(magic)), None)


def store(account, name, data):
    """Keep the file; returns its id, or None when it is not a PDF, PNG or JPEG."""
    media = kind_of(data)
    if not media:
        return None
    now = time.time()
    doc_id = uuid.uuid4().hex
    with db(account) as s:
        s.execute('DELETE FROM carbon_documents WHERE account=? AND NOT linked AND created<?', (account, now - ORPHAN_TTL))
        s.execute('INSERT INTO carbon_documents (id,account,name,type,size,created,linked,data) VALUES (?,?,?,?,?,?,?,?)',
                  (doc_id, account, (name or 'documento')[:200], media, len(data), now, False, data))
    return doc_id


def link(s, account, records):
    """Mark as linked the stored files that saved records now name."""
    pending = [r['id'] for r in s.execute('SELECT id FROM carbon_documents WHERE account=? AND NOT linked', (account,)).fetchall()]
    if not pending:
        return
    named = {f for item in records for f in ((item.get('origin') or {}).get('files') or []) if isinstance(f, str)}
    hits = [doc_id for doc_id in pending if doc_id in named]
    s.executemany('UPDATE carbon_documents SET linked=? WHERE account=? AND id=?', [(True, account, doc_id) for doc_id in hits])


def open_document(account, doc_id):
    with db(account) as s:
        row = s.execute('SELECT name,type,data FROM carbon_documents WHERE account=? AND id=?', (account, doc_id)).fetchone()
    if not row:
        raise HTTPException(404, 'El documento no está disponible.')
    return Response(bytes(row['data']), media_type=row['type'],
                    headers={'Content-Disposition': "inline; filename*=UTF-8''" + quote(row['name'])})
