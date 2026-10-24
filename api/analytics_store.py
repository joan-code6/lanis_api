"""Opaque account references and encrypted, short-lived diagnostic details.

PostHog receives references, never the ciphertext or the encryption key.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

import aiosqlite
from Crypto.Cipher import AES

from . import auth_db
from .identity import canonicalize_user_id

RETENTION_SECONDS = 7 * 24 * 60 * 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_accounts (
    user_id TEXT PRIMARY KEY,
    account_ref TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics_diagnostics (
    diagnostic_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    encrypted_payload TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS analytics_diagnostics_owner ON analytics_diagnostics(user_id, created_at);
CREATE INDEX IF NOT EXISTS analytics_diagnostics_expiry ON analytics_diagnostics(expires_at);
"""


def _key():
    # Domain-separated server secret; never a user's password or a session token.
    return hmac.new(auth_db._credential_key(), b'lanis:analytics-diagnostics:v1', hashlib.sha256).digest()


def _encrypt(payload):
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=secrets.token_bytes(12))
    cipher.update(b'lanis:analytics-diagnostics:v1')
    data, tag = cipher.encrypt_and_digest(json.dumps(payload, ensure_ascii=False).encode())
    return 'v1:' + base64.urlsafe_b64encode(cipher.nonce + tag + data).decode()


def _decrypt(value):
    if not value.startswith('v1:'):
        raise ValueError('Unsupported diagnostic encryption version')
    data = base64.urlsafe_b64decode(value[3:])
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=data[:12])
    cipher.update(b'lanis:analytics-diagnostics:v1')
    return json.loads(cipher.decrypt_and_verify(data[28:], data[12:28]))


async def account_reference(user_id):
    user_id = canonicalize_user_id(user_id)
    async with auth_db._lock:
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute('INSERT OR IGNORE INTO analytics_accounts VALUES (?, ?, ?)',
                             (user_id, 'la_' + uuid.uuid4().hex, int(time.time())))
            async with db.execute('SELECT account_ref FROM analytics_accounts WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return row[0]


async def resolve_account(account_ref):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        async with db.execute('SELECT user_id FROM analytics_accounts WHERE account_ref = ?', (account_ref,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def purge_expired():
    async with auth_db._lock:
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute('DELETE FROM analytics_diagnostics WHERE expires_at <= ?', (int(time.time()),))
            await db.commit()


async def save_diagnostic(user_id, payload):
    user_id = canonicalize_user_id(user_id)
    now = int(time.time())
    encrypted = _encrypt(payload)
    diagnostic_id = 'ld_' + uuid.uuid4().hex
    async with auth_db._lock:
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute('BEGIN IMMEDIATE')
            await db.execute('DELETE FROM analytics_diagnostics WHERE expires_at <= ?', (now,))
            async with db.execute('SELECT COUNT(*) FROM analytics_diagnostics WHERE user_id = ? AND created_at >= ?', (user_id, now - 60)) as cursor:
                if (await cursor.fetchone())[0] >= 20:
                    raise ValueError('Diagnostic rate limit exceeded')
            # Bound storage even when a broken client repeatedly reports errors.
            await db.execute('DELETE FROM analytics_diagnostics WHERE diagnostic_id IN (SELECT diagnostic_id FROM analytics_diagnostics WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET 99)', (user_id,))
            await db.execute('DELETE FROM analytics_diagnostics WHERE diagnostic_id IN (SELECT diagnostic_id FROM analytics_diagnostics ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET 4999)')
            await db.execute('INSERT INTO analytics_diagnostics (diagnostic_id, user_id, kind, encrypted_payload, request_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (diagnostic_id, user_id, payload['kind'], encrypted, payload.get('request_id', ''), now, now + RETENTION_SECONDS))
            await db.commit()
    return diagnostic_id


async def diagnostic_metadata(user_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT diagnostic_id, kind, created_at, expires_at FROM analytics_diagnostics WHERE user_id = ? AND expires_at > ? ORDER BY created_at DESC', (canonicalize_user_id(user_id), int(time.time()))) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def read_diagnostic(diagnostic_id):
    await purge_expired()
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM analytics_diagnostics WHERE diagnostic_id = ? AND expires_at > ?', (diagnostic_id, int(time.time()))) as cursor:
            row = await cursor.fetchone()
    if not row:
        return None
    return {'diagnostic_id': row['diagnostic_id'], 'user_id': row['user_id'],
            'created_at': row['created_at'], 'expires_at': row['expires_at'],
            'details': _decrypt(row['encrypted_payload'])}


async def export_metadata(user_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        async with db.execute('SELECT account_ref FROM analytics_accounts WHERE user_id = ?', (canonicalize_user_id(user_id),)) as cursor:
            row = await cursor.fetchone()
    return {'account_ref': row[0] if row else None, 'diagnostics': await diagnostic_metadata(user_id)}


async def diagnostic_owner(diagnostic_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        async with db.execute('SELECT user_id FROM analytics_diagnostics WHERE diagnostic_id = ? AND expires_at > ?', (diagnostic_id, int(time.time()))) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def diagnostics_for_request(request_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        async with db.execute('SELECT diagnostic_id, user_id FROM analytics_diagnostics WHERE request_id = ? AND expires_at > ? ORDER BY created_at', (request_id, int(time.time()))) as cursor:
            return [dict(diagnostic_id=row[0], user_id=row[1]) for row in await cursor.fetchall()]
