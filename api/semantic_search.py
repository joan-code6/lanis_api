"""
Semantic search engine using OpenAI-compatible embedding API.

Provides vector-based search across messages, courses, calendar events,
and modules using cosine similarity on text embeddings.
"""

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger("semantic_search")


# --- Embedding Client ---


class EmbeddingClient:
    """Calls an OpenAI-compatible /v1/embeddings endpoint."""

    def __init__(self, api_url: str, api_key: str, model: str = "text-embedding-3-small"):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""
        if not texts:
            return []

        response = requests.post(
            f"{self.api_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": texts,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        # Sort by index to guarantee order matches input
        embeddings = sorted(data["data"], key=lambda x: x["index"])
        return [e["embedding"] for e in embeddings]

    def embed_single(self, text: str) -> List[float]:
        """Embed a single text."""
        return self.embed([text])[0]


# --- Vector math (pure Python, no numpy needed) ---


def _normalize(vec: List[float]) -> List[float]:
    """L2-normalize a vector."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_norm = _normalize(a)
    b_norm = _normalize(b)
    return sum(x * y for x, y in zip(a_norm, b_norm, strict=True))


# --- Embedded document ---


@dataclass
class EmbeddedDocument:
    id: str
    text: str
    embedding: List[float]
    category: str
    title: str
    subtitle: str
    href: str
    icon: str
    created_at: float = field(default_factory=time.time)


# --- Per-user semantic index ---


class SemanticIndex:
    """In-memory vector index for a single user."""

    INDEX_TTL = 600  # rebuild after 10 minutes

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.documents: Dict[str, EmbeddedDocument] = {}
        self.last_built: float = 0

    def is_stale(self) -> bool:
        return time.time() - self.last_built > self.INDEX_TTL

    def is_empty(self) -> bool:
        return len(self.documents) == 0

    def add_documents(self, docs: List[EmbeddedDocument]) -> None:
        for doc in docs:
            self.documents[doc.id] = doc
        self.last_built = time.time()

    def search(self, query_embedding: List[float], top_k: int = 20) -> List[Tuple[EmbeddedDocument, float]]:
        """Return top_k results sorted by cosine similarity descending."""
        results: List[Tuple[EmbeddedDocument, float]] = []
        for doc in self.documents.values():
            score = cosine_similarity(query_embedding, doc.embedding)
            results.append((doc, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


# --- Search engine singleton ---


class SemanticSearchEngine:
    """Manages semantic indices and embedding clients across all users."""

    def __init__(self):
        self.indices: Dict[str, SemanticIndex] = {}
        self._client: Optional[EmbeddingClient] = None
        self._build_locks: Dict[str, asyncio.Lock] = {}
        self._last_access: Dict[str, float] = {}

    def _get_build_lock(self, user_id: str) -> asyncio.Lock:
        if user_id not in self._build_locks:
            self._build_locks[user_id] = asyncio.Lock()
        return self._build_locks[user_id]

    def _evict_stale(self) -> None:
        cutoff = time.time() - 2 * SemanticIndex.INDEX_TTL
        stale = [uid for uid, ts in self._last_access.items() if ts < cutoff]
        for uid in stale:
            self.indices.pop(uid, None)
            self._last_access.pop(uid, None)
            lock = self._build_locks.get(uid)
            if lock is None or not lock.locked():
                self._build_locks.pop(uid, None)
            logger.debug("Evicted stale semantic index for user %s", uid)

    def _get_client(self) -> Optional[EmbeddingClient]:
        if self._client is None:
            api_url = os.getenv("AI_API_URL")
            api_key = os.getenv("AI_API_KEY")
            model = os.getenv("AI_EMBEDDING_MODEL", "google/gemini-embedding-2")
            if api_url and api_key:
                self._client = EmbeddingClient(api_url, api_key, model)
                logger.info("Semantic search embedding client initialized (model=%s)", model)
            else:
                logger.debug("Semantic search disabled — AI_API_URL / AI_API_KEY not set")
        return self._client

    def get_index(self, user_id: str) -> SemanticIndex:
        self._last_access[user_id] = time.time()
        if user_id not in self.indices:
            self.indices[user_id] = SemanticIndex(user_id)
        return self.indices[user_id]

    def invalidate(self, user_id: str) -> None:
        self.indices.pop(user_id, None)
        self._last_access.pop(user_id, None)
        lock = self._build_locks.get(user_id)
        if lock is None or not lock.locked():
            self._build_locks.pop(user_id, None)

    # --- Document preparation helpers ---

    @staticmethod
    def _prepare_message_docs(messages: List[Dict]) -> List[Tuple[str, str, str, str, str]]:
        """Extract (id, text, title, subtitle, href) from message list."""
        docs = []
        for m in messages:
            subj = m.get("Betreff") or m.get("subject") or ""
            sender = m.get("Sender") or m.get("sender") or ""
            content = m.get("content") or m.get("Inhalt") or ""
            conv_id = m.get("Uniquid") or m.get("Id") or m.get("id") or ""

            text = f"Nachricht: {subj}. Von: {sender}. {content[:300]}"
            title = subj or "Kein Betreff"
            subtitle = f"Von: {sender}" if sender else "Nachricht"
            href = f"/messages?conversation={conv_id}"
            doc_id = f"sem-msg-{conv_id}"
            docs.append((doc_id, text, title, subtitle, href))
        return docs

    @staticmethod
    def _prepare_course_docs(courses: List[Dict]) -> List[Tuple[str, str, str, str, str]]:
        """Extract (id, text, title, subtitle, href) from course entries."""
        docs = []
        for c in courses:
            name = c.get("name") or c.get("thema") or ""
            teacher = c.get("teacher_full_name") or c.get("teacher_short") or ""
            thema = c.get("thema") or ""
            homework = c.get("homework") or ""
            book_id = c.get("book_id") or ""
            entry_id = c.get("entry_id") or ""

            text = f"Unterricht: {name}. Lehrer: {teacher}. Thema: {thema}. Hausaufgabe: {homework}"
            title = name or "Unbekannter Kurs"
            subtitle = teacher or thema
            href = f"/courses/{book_id}" if book_id else "/courses"
            doc_id = f"sem-crs-{entry_id or book_id}"
            docs.append((doc_id, text, title, subtitle, href))
        return docs

    @staticmethod
    def _prepare_calendar_docs(events: List[Dict]) -> List[Tuple[str, str, str, str, str]]:
        """Extract (id, text, title, subtitle, href) from calendar events."""
        docs = []
        for e in events:
            title = e.get("title") or ""
            desc = e.get("description") or ""
            location = e.get("location") or e.get("ort") or e.get("raum") or ""
            event_id = e.get("id") or ""
            start = e.get("start") or ""

            text = f"Termin: {title}. {desc}. Ort: {location}"
            subtitle = start[:10] if start else (e.get("category_name") or "")
            href = f"/calendar?event={event_id}"
            doc_id = f"sem-ev-{event_id}"
            docs.append((doc_id, text, title, subtitle, href))
        return docs

    @staticmethod
    def _prepare_module_docs(modules: List[Dict]) -> List[Tuple[str, str, str, str, str]]:
        """Extract (id, text, title, subtitle, href) from modules."""
        docs = []
        for m in modules:
            name = m.get("name") or ""
            url = m.get("url") or ""
            folders = m.get("folders") or []

            text = f"Modul: {name}. URL: {url}. Ordner: {', '.join(folders) if isinstance(folders, list) else str(folders)}"
            title = name or url or "Modul"
            subtitle = url or "Modul"
            href = "/dashboard"
            doc_id = f"sem-mod-{url or name}"
            docs.append((doc_id, text, title, subtitle, href))
        return docs

    async def search(
        self,
        user_id: str,
        query: str,
        auth_client: Any,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Perform semantic search across all data sources.

        Returns a list of results sorted by relevance, each containing:
        {id, category, title, subtitle, href, icon, score}
        """
        client = self._get_client()
        if not client:
            return []

        self._evict_stale()
        index = self.get_index(user_id)

        if index.is_empty() or index.is_stale():
            async with self._get_build_lock(user_id):
                if index.is_empty() or index.is_stale():
                    await self._build_index(user_id, index, auth_client)

        if index.is_empty():
            return []

        try:
            query_embedding = await run_in_threadpool(client.embed_single, query)
        except Exception as e:
            logger.error("Failed to embed query: %s", e)
            return []

        # Search
        results = index.search(query_embedding, top_k=top_k)

        known_icons = {
            "ChatBubbleLeftRightIcon",
            "AcademicCapIcon",
            "CalendarDaysIcon",
            "HomeIcon",
            "ClipboardDocumentListIcon",
        }

        return [
            {
                "id": doc.id,
                "category": doc.category,
                "title": doc.title,
                "subtitle": doc.subtitle,
                "href": doc.href,
                "icon": doc.icon if doc.icon in known_icons else "MagnifyingGlassIcon",
                "score": round(score, 4),
            }
            for doc, score in results
            if score > 0.25
        ]

    async def _build_index(
        self,
        user_id: str,
        index: SemanticIndex,
        auth_client: Any,
    ) -> None:
        """Fetch all data sources, embed them, and populate the index."""
        client = self._get_client()
        if not client:
            return

        all_docs: List[Tuple[str, str, str, str, str, str]] = []  # (id, text, title, subtitle, href, icon)

        # --- Messages ---
        try:
            msg_res = await run_in_threadpool(auth_client.nachrichten_get_headers, "All", 0)
            if msg_res.get("success") and msg_res.get("conversations"):
                messages = msg_res["conversations"]
                for doc_id, text, title, subtitle, href in self._prepare_message_docs(messages):
                    all_docs.append((doc_id, text, title, subtitle, href, "ChatBubbleLeftRightIcon"))
        except Exception as e:
            logger.warning("Failed to fetch messages for semantic index: %s", e)

        # --- Courses ---
        try:
            course_res = await run_in_threadpool(auth_client.meinunterricht_get_overview)
            if course_res.get("success") and course_res.get("entries"):
                courses = course_res["entries"]
                for doc_id, text, title, subtitle, href in self._prepare_course_docs(courses):
                    all_docs.append((doc_id, text, title, subtitle, href, "AcademicCapIcon"))
        except Exception as e:
            logger.warning("Failed to fetch courses for semantic index: %s", e)

        # --- Calendar ---
        try:
            cal_res = await run_in_threadpool(auth_client.kalender_get_events, "", "", "", "", "", "")
            if cal_res.get("success") and cal_res.get("events"):
                events = cal_res["events"]
                for doc_id, text, title, subtitle, href in self._prepare_calendar_docs(events):
                    all_docs.append((doc_id, text, title, subtitle, href, "CalendarDaysIcon"))
        except Exception as e:
            logger.warning("Failed to fetch calendar for semantic index: %s", e)

        # --- Modules ---
        try:
            mod_res = await run_in_threadpool(auth_client.apps_get_modules)
            if mod_res.get("success") and mod_res.get("modules"):
                modules = mod_res["modules"]
                for doc_id, text, title, subtitle, href in self._prepare_module_docs(modules):
                    all_docs.append((doc_id, text, title, subtitle, href, "HomeIcon"))
        except Exception as e:
            logger.warning("Failed to fetch modules for semantic index: %s", e)

        if not all_docs:
            logger.info("No documents found to index for user %s", user_id)
            return

        # Batch embed all texts
        texts = [doc[1] for doc in all_docs]
        try:
            embeddings = await run_in_threadpool(client.embed, texts)
        except Exception as e:
            logger.error("Failed to batch-embed documents: %s", e)
            return

        if len(embeddings) != len(all_docs):
            logger.error(
                "Embedding count mismatch for user %s: got %d, expected %d",
                user_id, len(embeddings), len(all_docs),
            )
            return

        # Build index
        docs = []
        for i, (doc_id, _text, title, subtitle, href, icon) in enumerate(all_docs):
            docs.append(EmbeddedDocument(
                id=doc_id,
                text=_text,
                embedding=embeddings[i],
                category=self._id_to_category(doc_id),
                title=title,
                subtitle=subtitle,
                href=href,
                icon=icon,
            ))

        index.documents.clear()
        index.add_documents(docs)
        logger.info("Semantic index built for user %s: %d documents", user_id, len(docs))

    @staticmethod
    def _id_to_category(doc_id: str) -> str:
        if doc_id.startswith("sem-msg"):
            return "Nachrichten"
        if doc_id.startswith("sem-crs"):
            return "Unterricht"
        if doc_id.startswith("sem-ev"):
            return "Kalender"
        if doc_id.startswith("sem-mod"):
            return "Module"
        if doc_id.startswith("sem-dsb"):
            return "Vertretungsplan"
        return "Sonstiges"


# Global singleton
semantic_engine = SemanticSearchEngine()
