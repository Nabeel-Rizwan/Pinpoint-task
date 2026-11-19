"""A tiny, dependency-free HTML DOM.

BeautifulSoup is lovely, but pulling a third-party dependency into an ingestion
service just to walk anchors is a supply-chain and ops cost we don't need for
this scope. :class:`Node` + :class:`DOMParser` give us exactly the tree we want:
find elements, read attributes, and get the visible text of a subtree.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# HTML void elements never have closing tags.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Subtrees whose text is never "visible" content and must be excluded from
# text_content (otherwise inline CSS/JS pollutes every extraction).
_NON_TEXT = {"script", "style", "template", "noscript", "svg"}

_WS_RE = re.compile(r"\s+")


class Node:
    """A minimal DOM node: tag, attributes, children and (for text) content."""

    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node] = []
        self.parent = parent
        self.text = ""

    def get(self, name: str, default: str | None = None) -> str | None:
        """Return an attribute value (case-insensitive key lookup)."""
        if name in self.attrs:
            return self.attrs[name]
        for key, value in self.attrs.items():
            if key.lower() == name.lower():
                return value
        return default

    def iter(self):
        """Depth-first walk over this node and all descendants."""
        yield self
        for child in self.children:
            yield from child.iter()

    def descendants(self, tag: str | None = None):
        """Yield descendant nodes, optionally filtered by tag name."""
        for node in self.iter():
            if node is self:
                continue
            if tag is None or node.tag == tag:
                yield node

    def find(self, tag: str) -> "Node | None":
        """First descendant with the given tag, or ``None``."""
        for node in self.descendants(tag):
            return node
        return None

    def find_all(self, tag: str) -> list["Node"]:
        """All descendants with the given tag."""
        return list(self.descendants(tag))

    @property
    def text_content(self) -> str:
        """Collapsed, visible text of this subtree (whitespace normalised).

        Skips ``script``/``style``/``template``/``noscript``/``svg`` subtrees so
        inline assets never leak into extracted text.
        """
        parts: list[str] = []

        def walk(node: "Node") -> None:
            for child in node.children:
                if child.tag in _NON_TEXT:
                    continue
                if child.tag == "#text":
                    parts.append(child.text)
                elif child.tag == "br":
                    parts.append(" ")
                else:
                    walk(child)

        walk(self)
        return _WS_RE.sub(" ", " ".join(parts)).strip()

    def ancestors(self):
        """Yield parent, grandparent, ... up to the root."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class DOMParser(HTMLParser):
    """Build a :class:`Node` tree from raw HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self._current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self._current)
        self._current.children.append(node)
        if tag not in _VOID:
            self._current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: (v or "") for k, v in attrs}, self._current)
        self._current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        # Walk up to the matching open tag; ignore stray closing tags.
        node = self._current
        while node is not self.root and node.tag != tag:
            node = node.parent  # type: ignore[assignment]
        if node is not self.root:
            self._current = node.parent  # type: ignore[assignment]

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            text_node = Node("#text", {}, self._current)
            text_node.text = data
            self._current.children.append(text_node)


def parse_html(html: str) -> Node:
    """Parse an HTML string and return the root :class:`Node`.

    The parser is intentionally lenient: real-world firm pages are frequently
    malformed and we would rather get a usable tree than raise.
    """
    parser = DOMParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is very forgiving
        pass
    return parser.root
