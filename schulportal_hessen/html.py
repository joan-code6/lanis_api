"""Project HTML helpers backed by selectolax.

Keeping the parser operations in one adapter preserves response behavior while
all HTML work is handled by selectolax's Lexbor backend.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode


def _matches(
    node: LexborNode,
    name: str | list[str] | tuple[str, ...] | None,
    attrs: dict[str, Any],
) -> bool:
    if name is not None:
        names = {name} if isinstance(name, str) else set(name)
        if node.tag not in names:
            return False

    attributes = node.attributes
    for key, expected in attrs.items():
        key = "class" if key == "class_" else key.rstrip("_")
        actual = attributes.get(key)
        if expected is True:
            if actual is None:
                return False
        elif key == "class" and isinstance(expected, str):
            if not all(item in (actual or "").split() for item in expected.split()):
                return False
        elif hasattr(expected, "search"):
            if actual is None or not expected.search(actual):
                return False
        elif actual != str(expected):
            return False
    return True


class HTMLNode:
    """Compatibility wrapper for the parser operations used by this project."""

    def __init__(self, node: LexborNode):
        self._node = node

    @property
    def attrs(self) -> dict[str, str]:
        return self._node.attributes

    @property
    def parent(self) -> HTMLNode | None:
        parent = self._node.parent
        return HTMLNode(parent) if parent is not None else None

    @property
    def stripped_strings(self) -> Iterator[str]:
        yield from self._stripped_strings(self._node)

    @classmethod
    def _stripped_strings(cls, node: LexborNode) -> Iterator[str]:
        for child in node.iter(include_text=True):
            if child.tag == "-text":
                value = " ".join(child.text().split())
                if value:
                    yield value
            else:
                yield from cls._stripped_strings(child)

    def get(self, key: str, default: Any = None) -> Any:
        return self._node.attributes.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self._node.attributes[key]

    def __str__(self) -> str:
        return self._node.html

    def get_text(
        self, separator: str = "", strip: bool = False, **_kwargs: Any
    ) -> str:
        return self._node.text(separator=separator, strip=strip)

    def select(self, selector: str) -> list[HTMLNode]:
        return [HTMLNode(node) for node in self._node.css(selector)]

    def select_one(self, selector: str) -> HTMLNode | None:
        node = self._node.css_first(selector)
        return HTMLNode(node) if node is not None else None

    def find(
        self,
        name: str | list[str] | tuple[str, ...] | None = None,
        attrs: dict[str, Any] | None = None,
        recursive: bool = True,
        **kwargs: Any,
    ) -> HTMLNode | None:
        matches = self.find_all(name, attrs, recursive=recursive, limit=1, **kwargs)
        return matches[0] if matches else None

    def find_all(
        self,
        name: str | list[str] | tuple[str, ...] | None = None,
        attrs: dict[str, Any] | None = None,
        recursive: bool = True,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[HTMLNode]:
        filters = dict(attrs or {})
        filters.update(kwargs)
        candidates = (
            self._node.css("*")
            if recursive
            else list(self._node.iter(include_text=False))
        )
        result: list[HTMLNode] = []
        for node in candidates:
            if _matches(node, name, filters):
                result.append(HTMLNode(node))
                if limit is not None and len(result) >= limit:
                    break
        return result

    def find_parent(
        self,
        name: str | None = None,
        attrs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> HTMLNode | None:
        filters = dict(attrs or {})
        filters.update(kwargs)
        node = self._node.parent
        while node is not None:
            if _matches(node, name, filters):
                return HTMLNode(node)
            node = node.parent
        return None

    def find_next_sibling(
        self,
        name: str | None = None,
        attrs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> HTMLNode | None:
        filters = dict(attrs or {})
        filters.update(kwargs)
        node = self._node.next
        while node is not None:
            if node.tag != "-text" and _matches(node, name, filters):
                return HTMLNode(node)
            node = node.next
        return None

    def decode_contents(self) -> str:
        return self._node.inner_html

    def decompose(self) -> None:
        self._node.decompose()

    def extract(self) -> None:
        self.decompose()


class HTMLParser(HTMLNode):
    """Parse HTML using selectolax's fast Lexbor backend."""

    def __init__(self, html: str | bytes, _parser: str | None = None):
        self._document = LexborHTMLParser(html)
        super().__init__(self._document.root)
