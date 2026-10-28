"""Appwrite v23 backend services for serverless LANiS Functions.

This module is deliberately independent from ``api/``.  It uses Appwrite
TablesDB for structured state, Storage for course attachments, Auth custom
tokens for optional client identity, and asynchronous Function executions for
durable background work.  Appwrite SDK and cryptography imports are lazy so
ordinary LANiS imports do not require the cloud dependencies.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """The Appwrite Function environment is invalid or incomplete."""


class DependencyError(RuntimeError):
    """A dependency needed only by the Appwrite backend is unavailable."""


class CipherError(RuntimeError):
    """Encrypted SPH data could not be authenticated or decrypted."""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")


def _env_value(environment: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environment.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class BackendSettings:
    endpoint: str
    project_id: str
    database_id: str = "lanis"
    refresh_tokens_table_id: str = "refresh_tokens"
    response_cache_table_id: str = "response_cache"
    user_metrics_table_id: str = "user_metrics"
    dsb_snapshots_table_id: str = "dsb_snapshots"
    file_metadata_table_id: str = "file_metadata"
    storage_bucket_id: str = "lanis-files"
    task_function_id: str = "lanis-worker"
    encryption_key: str = ""
    api_key: str | None = None

    def __post_init__(self) -> None:
        endpoint = self.endpoint.rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SettingsError("Appwrite endpoint must be an absolute HTTP(S) URL")
        object.__setattr__(self, "endpoint", endpoint)

        ids = {
            "project": self.project_id,
            "database": self.database_id,
            "refresh token table": self.refresh_tokens_table_id,
            "response cache table": self.response_cache_table_id,
            "metrics table": self.user_metrics_table_id,
            "DSB snapshot table": self.dsb_snapshots_table_id,
            "file metadata table": self.file_metadata_table_id,
            "storage bucket": self.storage_bucket_id,
            "task function": self.task_function_id,
        }
        invalid = [name for name, value in ids.items() if not _ID_RE.fullmatch(value)]
        if invalid:
            raise SettingsError(
                "Invalid Appwrite resource IDs: " + ", ".join(sorted(invalid))
            )
        if len(self.encryption_key) < 32:
            raise SettingsError(
                "LANIS_APPWRITE_ENCRYPTION_KEY must contain at least 32 characters"
            )

    @classmethod
    def from_env(
        cls, environment: Mapping[str, str] | None = None
    ) -> BackendSettings:
        env = os.environ if environment is None else environment
        endpoint = _env_value(
            env,
            "LANIS_APPWRITE_ENDPOINT",
            "APPWRITE_FUNCTION_API_ENDPOINT",
        )
        project_id = _env_value(
            env,
            "LANIS_APPWRITE_PROJECT_ID",
            "APPWRITE_FUNCTION_PROJECT_ID",
        )
        encryption_key = _env_value(env, "LANIS_APPWRITE_ENCRYPTION_KEY")
        missing = []
        if not endpoint:
            missing.append("LANIS_APPWRITE_ENDPOINT/APPWRITE_FUNCTION_API_ENDPOINT")
        if not project_id:
            missing.append("LANIS_APPWRITE_PROJECT_ID/APPWRITE_FUNCTION_PROJECT_ID")
        if not encryption_key:
            missing.append("LANIS_APPWRITE_ENCRYPTION_KEY")
        if missing:
            raise SettingsError("Missing Appwrite settings: " + ", ".join(missing))

        return cls(
            endpoint=endpoint or "",
            project_id=project_id or "",
            database_id=_env_value(env, "LANIS_APPWRITE_DATABASE_ID") or "lanis",
            refresh_tokens_table_id=_env_value(
                env, "LANIS_APPWRITE_REFRESH_TOKENS_TABLE_ID"
            )
            or "refresh_tokens",
            response_cache_table_id=_env_value(
                env, "LANIS_APPWRITE_RESPONSE_CACHE_TABLE_ID"
            )
            or "response_cache",
            user_metrics_table_id=_env_value(
                env, "LANIS_APPWRITE_USER_METRICS_TABLE_ID"
            )
            or "user_metrics",
            dsb_snapshots_table_id=_env_value(
                env, "LANIS_APPWRITE_DSB_SNAPSHOTS_TABLE_ID"
            )
            or "dsb_snapshots",
            file_metadata_table_id=_env_value(
                env, "LANIS_APPWRITE_FILE_METADATA_TABLE_ID"
            )
            or "file_metadata",
            storage_bucket_id=_env_value(
                env, "LANIS_APPWRITE_STORAGE_BUCKET_ID"
            )
            or "lanis-files",
            task_function_id=_env_value(
                env, "LANIS_APPWRITE_TASK_FUNCTION_ID"
            )
            or "lanis-worker",
            encryption_key=encryption_key or "",
            api_key=_env_value(
                env,
                "LANIS_APPWRITE_API_KEY",
                "APPWRITE_FUNCTION_API_KEY",
            ),
        )


@dataclass(frozen=True)
class SdkServices:
    client: Any
    tables_db: Any
    storage: Any
    functions: Any
    users: Any
    query: Any
    input_file: Any


class _SdkFactory:
    def __init__(
        self,
        settings: BackendSettings,
        dynamic_key: str | None = None,
        services: SdkServices | None = None,
    ) -> None:
        self.settings = settings
        self._api_key = dynamic_key or settings.api_key
        self._services = services
        self._lock = threading.RLock()

    def services(self) -> SdkServices:
        with self._lock:
            if self._services is not None:
                return self._services
            try:
                from appwrite.client import Client
                from appwrite.input_file import InputFile
                from appwrite.query import Query
                from appwrite.services.functions import Functions
                from appwrite.services.storage import Storage
                from appwrite.services.tables_db import TablesDB
                from appwrite.services.users import Users
            except ImportError as exc:
                raise DependencyError(
                    "The Appwrite backend requires appwrite>=23,<24"
                ) from exc

            client = Client()
            client.set_endpoint(self.settings.endpoint)
            client.set_project(self.settings.project_id)
            if self._api_key:
                client.set_key(self._api_key)
            self._services = SdkServices(
                client=client,
                tables_db=TablesDB(client),
                storage=Storage(client),
                functions=Functions(client),
                users=Users(client),
                query=Query,
                input_file=InputFile,
            )
            return self._services

    def set_api_key(self, api_key: str) -> None:
        if not api_key or not api_key.strip():
            raise SettingsError("Appwrite API key cannot be empty")
        with self._lock:
            self._api_key = api_key.strip()
            client = self.services().client
            if not hasattr(client, "set_key"):
                raise DependencyError("Configured Appwrite client cannot set an API key")
            client.set_key(self._api_key)


class _Cipher:
    """AES-GCM encryption using the project's existing PyCryptodome dependency."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise SettingsError("Appwrite encryption key is too short")
        try:
            from Crypto.Cipher import AES
            from Crypto.Random import get_random_bytes
        except ImportError as exc:
            raise DependencyError(
                "Encrypted Appwrite state requires pycryptodome"
            ) from exc
        self._aes = AES
        self._random_bytes = get_random_bytes
        self._key = hashlib.sha256(secret.encode()).digest()

    def encrypt(self, plaintext: str) -> str:
        nonce = self._random_bytes(12)
        cipher = self._aes.new(self._key, self._aes.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode())
        payload = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode()
        return f"v1:{payload}"

    def decrypt(self, value: str) -> str:
        try:
            version, encoded = value.split(":", 1)
            if version != "v1":
                raise ValueError("unsupported encrypted value version")
            raw = base64.urlsafe_b64decode(encoded.encode())
            nonce, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
            cipher = self._aes.new(self._key, self._aes.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode()
        except (IndexError, TypeError, ValueError, UnicodeError) as exc:
            raise CipherError("Stored SPH data failed authentication") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"Unsupported datetime value: {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_id(namespace: str, value: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).hexdigest()
    return f"d{digest[:35]}"


def _user_key(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _status(exc: BaseException, code: int) -> bool:
    return getattr(exc, "code", None) == code or getattr(
        exc, "status_code", None
    ) == code


def _mapping(value: Any) -> dict[str, Any]:
    """Normalize raw responses and Appwrite v23 Pydantic response models."""
    if isinstance(value, dict):
        payload = dict(value)
    else:
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            payload = dict(to_dict())
        else:
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                payload = dict(model_dump(by_alias=True, mode="json"))
            else:
                raise TypeError(
                    f"Unsupported Appwrite response: {type(value).__name__}"
                )
    row_data = payload.pop("data", None)
    if isinstance(row_data, dict):
        return {**row_data, **payload}
    return payload


class _Table:
    def __init__(self, tables_db: Any, query: Any, database_id: str, table_id: str):
        self._db = tables_db
        self._query_type = query
        self._database_id = database_id
        self._table_id = table_id

    def _query(self, method: str, *args: Any) -> Any:
        return getattr(self._query_type, method)(*args)

    async def _get(self, row_id: str) -> dict[str, Any] | None:
        try:
            result = await asyncio.to_thread(
                self._db.get_row,
                database_id=self._database_id,
                table_id=self._table_id,
                row_id=row_id,
            )
        except Exception as exc:
            if _status(exc, 404):
                return None
            raise
        return _mapping(result)

    async def _upsert(self, row_id: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._db.upsert_row,
            database_id=self._database_id,
            table_id=self._table_id,
            row_id=row_id,
            data=data,
        )

    async def _delete(self, row_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._db.delete_row,
                database_id=self._database_id,
                table_id=self._table_id,
                row_id=row_id,
            )
        except Exception as exc:
            if not _status(exc, 404):
                raise

    async def _list(self, queries: list[Any] | None = None) -> list[dict[str, Any]]:
        result = await asyncio.to_thread(
            self._db.list_rows,
            database_id=self._database_id,
            table_id=self._table_id,
            queries=queries or [],
        )
        return [_mapping(row) for row in _mapping(result).get("rows", [])]

    async def _all(
        self, queries: list[Any] | None = None, page_size: int = 100
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_queries = list(queries or [])
            page_queries.extend(
                [self._query("limit", page_size), self._query("offset", offset)]
            )
            page = await self._list(page_queries)
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size


@dataclass(frozen=True)
class SessionCredentials:
    user_id: str
    school_id: str
    username: str
    password: str
    created_at: str
    expires_at: str
    refresh_token: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.refresh_token,
            "user_id": self.user_id,
            "school_id": self.school_id,
            "username": self.username,
            "password": self.password,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class CredentialStore(_Table):
    def __init__(
        self,
        tables_db: Any,
        query: Any,
        settings: BackendSettings,
        cipher: _Cipher,
        ttl_days: int = 90,
    ) -> None:
        super().__init__(
            tables_db,
            query,
            settings.database_id,
            settings.refresh_tokens_table_id,
        )
        self._cipher = cipher
        self._ttl_days = ttl_days

    async def store_refresh_token(
        self, user_id: str, school_id: str, username: str, password: str
    ) -> str:
        token = uuid.uuid4().hex
        now = _now()
        await self._upsert(
            _row_id("refresh-token", token),
            {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "user_key": _user_key(user_id),
                "user_id": user_id,
                "school_id": school_id,
                "username": username,
                "password_encrypted": self._cipher.encrypt(password),
                "created_at": _iso(now),
                "expires_at": _iso(now + timedelta(days=self._ttl_days)),
            },
        )
        return token

    def _credentials(
        self, row: dict[str, Any], token: str | None = None
    ) -> SessionCredentials:
        return SessionCredentials(
            user_id=row["user_id"],
            school_id=row["school_id"],
            username=row["username"],
            password=self._cipher.decrypt(row["password_encrypted"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            refresh_token=token,
        )

    async def get_refresh_token(self, token: str) -> dict[str, Any] | None:
        row = await self._get(_row_id("refresh-token", token))
        if not row:
            return None
        if _parse_time(row["expires_at"]) <= _now():
            await self.delete_refresh_token(token)
            return None
        return self._credentials(row, token).as_dict()

    async def get_credentials(self, user_id: str) -> SessionCredentials | None:
        rows = await self._list(
            [
                self._query("equal", "user_key", [_user_key(user_id)]),
                self._query("greater_than", "expires_at", _iso(_now())),
                self._query("order_desc", "created_at"),
                self._query("limit", 1),
            ]
        )
        return self._credentials(rows[0]) if rows else None

    async def get_refresh_token_by_user_id(
        self, user_id: str
    ) -> dict[str, Any] | None:
        credentials = await self.get_credentials(user_id)
        return None if credentials is None else credentials.as_dict()

    async def delete_refresh_token(self, token: str) -> None:
        await self._delete(_row_id("refresh-token", token))

    async def delete_user_tokens(self, user_id: str) -> None:
        rows = await self._all(
            [self._query("equal", "user_key", [_user_key(user_id)])]
        )
        await asyncio.gather(*(self._delete(row["$id"]) for row in rows))


class ResponseCache(_Table):
    def __init__(
        self,
        tables_db: Any,
        query: Any,
        settings: BackendSettings,
        ttl_seconds: int = 10 * 60,
        long_ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        super().__init__(
            tables_db,
            query,
            settings.database_id,
            settings.response_cache_table_id,
        )
        self._ttl = ttl_seconds
        self._long_ttl = long_ttl_seconds

    @staticmethod
    def _id(user_id: str, endpoint: str, params: str) -> str:
        return _row_id("response-cache", f"{user_id}:{endpoint}:{params}")

    async def _entry(
        self, user_id: str, endpoint: str, params: str
    ) -> tuple[Any, bool] | None:
        row_id = self._id(user_id, endpoint, params)
        row = await self._get(row_id)
        if not row:
            return None
        now = _now()
        if _parse_time(row["expires_at"]) <= now:
            await self._delete(row_id)
            return None
        stale = bool(row.get("is_long_term")) and _parse_time(row["stale_at"]) <= now
        return json.loads(row["data_json"]), stale

    async def get(
        self, user_id: str, endpoint: str, params: str = ""
    ) -> Any | None:
        entry = await self._entry(user_id, endpoint, params)
        return None if entry is None else entry[0]

    async def get_with_revalidate(
        self, user_id: str, endpoint: str, params: str = ""
    ) -> tuple[Any | None, bool]:
        entry = await self._entry(user_id, endpoint, params)
        return (None, False) if entry is None else entry

    async def set(
        self,
        user_id: str,
        endpoint: str,
        data: Any,
        params: str = "",
        is_long_term: bool = False,
    ) -> None:
        now = _now()
        ttl = self._long_ttl if is_long_term else self._ttl
        await self._upsert(
            self._id(user_id, endpoint, params),
            {
                "user_key": _user_key(user_id),
                "endpoint": endpoint,
                "params_hash": hashlib.sha256(params.encode()).hexdigest(),
                "data_json": _json(data),
                "created_at": _iso(now),
                "stale_at": _iso(now + timedelta(seconds=ttl // 2)),
                "expires_at": _iso(now + timedelta(seconds=ttl)),
                "is_long_term": is_long_term,
            },
        )

    async def invalidate_user(self, user_id: str) -> None:
        rows = await self._all(
            [self._query("equal", "user_key", [_user_key(user_id)])]
        )
        await asyncio.gather(*(self._delete(row["$id"]) for row in rows))

    get_cached = get
    get_cached_with_revalidate = get_with_revalidate
    set_cache = set
    invalidate_user_cache = invalidate_user


class MetricsStore(_Table):
    def __init__(self, tables_db: Any, query: Any, settings: BackendSettings):
        super().__init__(
            tables_db,
            query,
            settings.database_id,
            settings.user_metrics_table_id,
        )

    @staticmethod
    def _id(school_id: str, login: str) -> str:
        return _row_id("metrics", f"{school_id}:{login}")

    async def upsert_user(
        self, school_id: str, login: str, user_data: dict[str, Any]
    ) -> tuple[bool, bool]:
        row_id = self._id(school_id, login)
        current = await self._get(row_id)
        data_hash = hashlib.sha256(
            json.dumps(user_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if current and current.get("data_hash") == data_hash:
            return False, False
        now = _iso(_now())
        await self._upsert(
            row_id,
            {
                "school_id": school_id,
                "login": login,
                "data_hash": data_hash,
                "user_data_json": _json(user_data),
                "first_seen": current.get("first_seen", now) if current else now,
                "last_updated": now,
                "update_count": int(current.get("update_count", 0)) + 1
                if current
                else 1,
            },
        )
        return current is None, True

    async def get_stats(self) -> dict[str, Any]:
        rows = await self._all()
        cutoff = _now() - timedelta(days=1)
        return {
            "total_users": len(rows),
            "unique_schools": len({row["school_id"] for row in rows}),
            "recent_updates_24h": sum(
                _parse_time(row["last_updated"]) > cutoff for row in rows
            ),
            "new_users_today": sum(
                _parse_time(row["first_seen"]) > cutoff for row in rows
            ),
            "provider": "appwrite",
        }


class SnapshotStore(_Table):
    def __init__(self, tables_db: Any, query: Any, settings: BackendSettings):
        super().__init__(
            tables_db,
            query,
            settings.database_id,
            settings.dsb_snapshots_table_id,
        )

    @staticmethod
    def _id(school_id: str, target_date: date) -> str:
        return _row_id("dsb", f"{school_id}:{target_date.isoformat()}")

    async def store(
        self,
        school_id: str,
        snapshot_data: dict[str, Any],
        entry_count: int,
        *,
        fetched_at: datetime | None = None,
    ) -> None:
        now = fetched_at or _now()
        await self._upsert(
            self._id(school_id, now.date()),
            {
                "school_id": school_id,
                "fetch_date": now.date().isoformat(),
                "fetch_time": _iso(now),
                "data_json": _json(snapshot_data),
                "entry_count": entry_count,
            },
        )

    async def get(
        self, target_date: date, school_id: str | None = None
    ) -> dict[str, Any] | None:
        if school_id:
            row = await self._get(self._id(school_id, target_date))
        else:
            rows = await self._list(
                [
                    self._query("equal", "fetch_date", [target_date.isoformat()]),
                    self._query("order_desc", "fetch_time"),
                    self._query("limit", 1),
                ]
            )
            row = rows[0] if rows else None
        return None if row is None else json.loads(row["data_json"])

    store_snapshot = store
    get_snapshot = get


@dataclass(frozen=True)
class FileMetadata:
    file_hash: str
    storage_file_id: str
    source_url: str | None
    content_type: str | None
    filename: str | None
    status: str
    updated_at: datetime

    @property
    def download_url(self) -> str | None:
        return self.source_url


class FileStore(_Table):
    def __init__(
        self,
        tables_db: Any,
        storage: Any,
        query: Any,
        input_file: Any,
        settings: BackendSettings,
        cipher: _Cipher,
    ) -> None:
        super().__init__(
            tables_db,
            query,
            settings.database_id,
            settings.file_metadata_table_id,
        )
        self._storage = storage
        self._input_file = input_file
        self._bucket_id = settings.storage_bucket_id
        self._cipher = cipher

    @staticmethod
    def hash_url(source_url: str) -> str:
        return hashlib.sha256(source_url.encode()).hexdigest()

    @staticmethod
    def _id(file_hash: str) -> str:
        return _row_id("file", file_hash)

    @staticmethod
    def _safe_name(filename: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", filename) or "download"

    async def get_metadata(self, file_hash: str) -> FileMetadata | None:
        row = await self._get(self._id(file_hash))
        if not row:
            return None
        encrypted_source = row.get("source_url_encrypted")
        return FileMetadata(
            file_hash=row["file_hash"],
            storage_file_id=row["storage_file_id"],
            source_url=(
                self._cipher.decrypt(encrypted_source) if encrypted_source else None
            ),
            content_type=row.get("content_type"),
            filename=row.get("filename"),
            status=row["status"],
            updated_at=_parse_time(row["updated_at"]),
        )

    async def mark_pending(self, file_hash: str, source_url: str) -> None:
        if not source_url:
            raise ValueError("source_url cannot be empty")
        current = await self._get(self._id(file_hash)) or {}
        if current.get("status") == "ready":
            return
        await self._upsert(
            self._id(file_hash),
            {
                "file_hash": file_hash,
                "storage_file_id": current.get("storage_file_id", self._id(file_hash)),
                "source_url_encrypted": self._cipher.encrypt(source_url),
                "content_type": current.get("content_type"),
                "filename": current.get("filename"),
                "status": "pending",
                "updated_at": _iso(_now()),
            },
        )

    async def unmark_pending(self, file_hash: str) -> None:
        current = await self._get(self._id(file_hash))
        if not current or current.get("status") != "pending":
            return
        await self._upsert(
            self._id(file_hash),
            {
                "file_hash": current["file_hash"],
                "storage_file_id": current["storage_file_id"],
                "source_url_encrypted": current.get("source_url_encrypted"),
                "content_type": current.get("content_type"),
                "filename": current.get("filename"),
                "status": "failed",
                "updated_at": _iso(_now()),
            },
        )

    async def is_pending(self, file_hash: str) -> bool:
        metadata = await self.get_metadata(file_hash)
        return metadata is not None and metadata.status == "pending"

    async def is_cached(self, file_hash: str) -> bool:
        metadata = await self.get_metadata(file_hash)
        if not metadata or metadata.status != "ready":
            return False
        try:
            await asyncio.to_thread(
                self._storage.get_file,
                bucket_id=self._bucket_id,
                file_id=metadata.storage_file_id,
            )
        except Exception as exc:
            if _status(exc, 404):
                return False
            raise
        return True

    async def save(
        self,
        file_hash: str,
        content: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        storage_id = self._id(file_hash)
        try:
            await asyncio.to_thread(
                self._storage.get_file,
                bucket_id=self._bucket_id,
                file_id=storage_id,
            )
        except Exception as exc:
            if not _status(exc, 404):
                raise
            upload = self._input_file.from_bytes(
                content,
                self._safe_name(filename),
                content_type or "application/octet-stream",
            )
            # Metadata remains pending if this upload raises.
            await asyncio.to_thread(
                self._storage.create_file,
                bucket_id=self._bucket_id,
                file_id=storage_id,
                file=upload,
            )

        await self._upsert(
            self._id(file_hash),
            {
                "file_hash": file_hash,
                "storage_file_id": storage_id,
                "source_url_encrypted": None,
                "content_type": content_type or "application/octet-stream",
                "filename": self._safe_name(filename),
                "status": "ready",
                "updated_at": _iso(_now()),
            },
        )

    async def get_content(self, file_hash: str) -> bytes:
        metadata = await self.get_metadata(file_hash)
        if not metadata or metadata.status != "ready":
            raise FileNotFoundError(file_hash)
        return await asyncio.to_thread(
            self._storage.get_file_download,
            bucket_id=self._bucket_id,
            file_id=metadata.storage_file_id,
        )

    async def delete(self, file_hash: str) -> None:
        metadata = await self.get_metadata(file_hash)
        if metadata:
            try:
                await asyncio.to_thread(
                    self._storage.delete_file,
                    bucket_id=self._bucket_id,
                    file_id=metadata.storage_file_id,
                )
            except Exception as exc:
                if not _status(exc, 404):
                    raise
        await self._delete(self._id(file_hash))

    get_file_hash = hash_url
    get_file = get_metadata
    is_file_cached = is_cached
    is_file_pending = is_pending
    save_file = save


@dataclass(frozen=True)
class IdentityToken:
    user_id: str
    secret: str
    expire: str


class IdentityService:
    def __init__(self, users: Any, token_expire_seconds: int = 15 * 60):
        self._users = users
        self._token_expire_seconds = token_expire_seconds

    async def ensure_user_and_create_token(
        self, school_id: str, username: str
    ) -> IdentityToken:
        user_id = _row_id("identity", f"{school_id}:{username}")
        try:
            await asyncio.to_thread(self._users.get, user_id=user_id)
        except Exception as exc:
            if not _status(exc, 404):
                raise
            try:
                await asyncio.to_thread(
                    self._users.create, user_id=user_id, name=username[:128]
                )
            except Exception as create_exc:
                if not _status(create_exc, 409):
                    raise
        result = _mapping(
            await asyncio.to_thread(
                self._users.create_token,
                user_id=user_id,
                length=64,
                expire=self._token_expire_seconds,
            )
        )
        if not result.get("secret") or not result.get("expire"):
            raise RuntimeError("Appwrite returned an incomplete custom token")
        return IdentityToken(user_id, str(result["secret"]), str(result["expire"]))


class FunctionDispatcher:
    def __init__(self, functions: Any, settings: BackendSettings):
        self._functions = functions
        self._function_id = settings.task_function_id

    async def dispatch(
        self,
        task: str,
        payload: dict[str, Any],
        *,
        scheduled_at: datetime | str | None = None,
    ) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", task or ""):
            raise ValueError("task contains unsupported characters")
        schedule = _iso(scheduled_at) if isinstance(scheduled_at, datetime) else scheduled_at
        result = _mapping(
            await asyncio.to_thread(
                self._functions.create_execution,
                function_id=self._function_id,
                body=_json({"version": 1, "task": task, "payload": payload}),
                xasync=True,
                path=f"/tasks/{task}",
                scheduled_at=schedule,
            )
        )
        execution_id = result.get("$id") or result.get("id")
        if not execution_id:
            raise RuntimeError("Appwrite returned no execution ID")
        return str(execution_id)


@dataclass(frozen=True)
class AppwriteBackend:
    settings: BackendSettings
    credentials: CredentialStore
    cache: ResponseCache
    metrics: MetricsStore
    snapshots: SnapshotStore
    files: FileStore
    identity: IdentityService
    dispatcher: FunctionDispatcher
    _factory: _SdkFactory

    def set_api_key(self, api_key: str) -> None:
        self._factory.set_api_key(api_key)


def build_backend(
    settings: BackendSettings,
    *,
    dynamic_key: str | None = None,
    services: SdkServices | None = None,
    cipher: _Cipher | None = None,
) -> AppwriteBackend:
    """Build a backend; dependency injection arguments are intended for tests."""
    factory = _SdkFactory(settings, dynamic_key=dynamic_key, services=services)
    sdk = factory.services()
    encryption = cipher or _Cipher(settings.encryption_key)
    return AppwriteBackend(
        settings=settings,
        credentials=CredentialStore(sdk.tables_db, sdk.query, settings, encryption),
        cache=ResponseCache(sdk.tables_db, sdk.query, settings),
        metrics=MetricsStore(sdk.tables_db, sdk.query, settings),
        snapshots=SnapshotStore(sdk.tables_db, sdk.query, settings),
        files=FileStore(
            sdk.tables_db,
            sdk.storage,
            sdk.query,
            sdk.input_file,
            settings,
            encryption,
        ),
        identity=IdentityService(sdk.users),
        dispatcher=FunctionDispatcher(sdk.functions, settings),
        _factory=factory,
    )


_backend: AppwriteBackend | None = None
_backend_lock = threading.RLock()


def get_backend(dynamic_key: str | None = None) -> AppwriteBackend:
    """Return the process-global backend and apply a Function dynamic key."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = build_backend(
                BackendSettings.from_env(), dynamic_key=dynamic_key
            )
        elif dynamic_key:
            _backend.set_api_key(dynamic_key)
        return _backend


def _reset_backend_for_tests() -> None:
    global _backend
    with _backend_lock:
        _backend = None


def reset_backend() -> None:
    """Reset the lazy process-local client (useful for tests and reloads)."""
    _reset_backend_for_tests()


__all__ = [
    "AppwriteBackend",
    "BackendSettings",
    "CipherError",
    "DependencyError",
    "FileMetadata",
    "IdentityToken",
    "SdkServices",
    "SessionCredentials",
    "SettingsError",
    "build_backend",
    "get_backend",
    "reset_backend",
]
