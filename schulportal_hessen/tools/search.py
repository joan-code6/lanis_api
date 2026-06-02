from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Iterable


def normalize_search_text(value: Any) -> str:
    """Normalize text for tolerant search matching.

    This strips accents, lowercases, and collapses whitespace so that
    user queries can match across minor text variations.
    """
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def text_matches_query(text: Any, query: Any, *, min_similarity: float = 0.82) -> bool:
    """Return True when text matches query with tolerant heuristics."""
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return True

    normalized_text = normalize_search_text(text)
    if not normalized_text:
        return False

    if normalized_query in normalized_text:
        return True

    query_tokens = normalized_query.split()
    if query_tokens and all(token in normalized_text for token in query_tokens):
        return True

    text_tokens = normalized_text.split()
    if query_tokens and text_tokens:
        token_ok = True
        for token in query_tokens:
            if any(token in word for word in text_tokens):
                continue
            if any(
                difflib.SequenceMatcher(None, token, word).ratio() >= 0.9
                for word in text_tokens
            ):
                continue
            token_ok = False
            break
        if token_ok:
            return True

    if len(normalized_query) >= 4:
        if any(
            abs(len(word) - len(normalized_query)) <= 2
            and difflib.SequenceMatcher(None, word, normalized_query).ratio() >= min_similarity
            for word in text_tokens
        ):
            return True

    return False


def iter_searchable_strings(value: Any) -> Iterable[str]:
    """Yield string fragments from nested values for broad search matching."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_searchable_strings(inner)
        return
    if isinstance(value, (list, tuple, set)):
        for inner in value:
            yield from iter_searchable_strings(inner)
        return
    if isinstance(value, (int, float, bool)):
        yield str(value)


def value_matches_query(value: Any, query: Any) -> bool:
    """Return True when any searchable text in the value matches the query."""
    combined = " ".join(iter_searchable_strings(value))
    return text_matches_query(combined, query)
