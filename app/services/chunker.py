import hashlib
import logging
import re
from dataclasses import dataclass
from typing import List

import tiktoken
from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

_SKIP = frozenset(
    ["script", "style", "nav", "footer", "aside", "form",
     "noscript", "iframe", "button", "input", "select", "option",
     "meta", "link", "head"]
)
_HEADING = frozenset(["h1", "h2", "h3", "h4", "h5", "h6"])
_INLINE = frozenset(["span", "a", "strong", "em", "b", "i", "u",
                      "code", "small", "abbr", "time", "label"])


@dataclass
class PageChunk:
    text: str
    url: str
    title: str
    section_path: str
    chunk_type: str       # paragraph | list | table | mixed
    chunk_index: int
    token_count: int
    content_hash: str


class SemanticChunker:
    def __init__(self, target: int = 450, overlap: int = 50, minimum: int = 40):
        self.target = target
        self.overlap = overlap
        self.minimum = minimum
        self._enc = tiktoken.get_encoding("cl100k_base")

    def tokens(self, text: str) -> int:
        return len(self._enc.encode(text))

    # ── Public API ──────────────────────────────────────────────────

    def chunk_page(self, html: str, url: str, title: str) -> List[PageChunk]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(_SKIP):
            tag.decompose()

        events: list[tuple] = []           # (type, text, path)
        h_stack: list[tuple[int, str]] = []
        self._walk(soup.find("body") or soup, events, h_stack)

        return self._build_chunks(events, url, title)

    # ── DOM walker ──────────────────────────────────────────────────

    def _walk(self, node: Tag, events: list, h_stack: list) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    events.append(("text", text, _path(h_stack)))
                continue
            if not isinstance(child, Tag):
                continue

            tag = child.name.lower()

            if tag in _SKIP:
                continue

            if tag in _HEADING:
                level = int(tag[1])
                text = child.get_text(" ", strip=True)
                if not text:
                    continue
                # Update heading stack in-place so siblings see the new context
                h_stack[:] = [(l, t) for l, t in h_stack if l < level] + [(level, text)]
                events.append(("heading", text, _path(h_stack)))

            elif tag == "table":
                text = _table_to_text(child)
                if text:
                    events.append(("table", text, _path(h_stack)))

            elif tag in ("ul", "ol", "dl"):
                if not child.find_parent(("ul", "ol", "dl")):  # top-level lists only
                    text = _list_to_text(child)
                    if text:
                        events.append(("list", text, _path(h_stack)))

            elif tag == "p":
                text = child.get_text(" ", strip=True)
                if text:
                    events.append(("paragraph", text, _path(h_stack)))

            elif tag in _INLINE:
                # Inline elements: capture direct text, skip container recursion
                text = child.get_text(" ", strip=True)
                if text:
                    events.append(("text", text, _path(h_stack)))

            else:
                # Generic container (div, section, article, main, …) — recurse
                self._walk(child, events, h_stack)

    # ── Chunk builder ───────────────────────────────────────────────

    def _build_chunks(self, events: list, url: str, title: str) -> List[PageChunk]:
        chunks: List[PageChunk] = []
        buffer: list[tuple] = []   # (type, text, path)
        buf_t = 0
        idx = 0

        def flush() -> None:
            nonlocal idx, buf_t
            if not buffer:
                return
            path = buffer[0][2]
            body = "\n\n".join(b[1] for b in buffer)
            full = f"[{path}]\n\n{body}" if path else body
            t = self.tokens(full)
            if t < self.minimum:
                buffer.clear(); buf_t = 0; return
            c_type = buffer[0][0] if len({b[0] for b in buffer}) == 1 else "mixed"
            chunks.append(PageChunk(
                text=full, url=url, title=title,
                section_path=path, chunk_type=c_type,
                chunk_index=idx, token_count=t,
                content_hash=hashlib.md5(full.encode()).hexdigest(),
            ))
            idx += 1
            buffer.clear(); buf_t = 0

        for ev_type, text, path in events:

            # Headings mark section boundaries — flush and move on
            if ev_type == "heading":
                flush()
                continue

            # Tables are atomic
            if ev_type == "table":
                flush()
                full = f"[{path}]\n\n{text}" if path else text
                t = self.tokens(full)
                if t >= self.minimum:
                    chunks.append(PageChunk(
                        text=full, url=url, title=title,
                        section_path=path, chunk_type="table",
                        chunk_index=idx, token_count=t,
                        content_hash=hashlib.md5(full.encode()).hexdigest(),
                    ))
                    idx += 1
                continue

            block_t = self.tokens(text)

            # Section change — flush before starting new section buffer
            if buffer and buffer[0][2] != path:
                flush()

            # Oversized single block — split at sentence boundaries
            if block_t > self.target * 2:
                flush()
                for sub in self._split_large(text, path, ev_type, url, title, idx):
                    chunks.append(sub)
                    idx += 1
                continue

            # Buffer overflow — flush, carry one sentence as overlap
            if buf_t + block_t > self.target and buffer:
                carry = [buffer[-1]] if self.tokens(buffer[-1][1]) <= self.overlap else []
                flush()
                if carry:
                    buffer.extend(carry)
                    buf_t = self.tokens(carry[0][1])

            buffer.append((ev_type, text, path))
            buf_t += block_t

        flush()
        return chunks

    def _split_large(
        self, text: str, path: str, kind: str, url: str, title: str, start_idx: int
    ) -> List[PageChunk]:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks: List[PageChunk] = []
        buf: list[str] = []
        buf_t = 0

        def emit():
            body = " ".join(buf)
            full = f"[{path}]\n\n{body}" if path else body
            t = self.tokens(full)
            if t >= self.minimum:
                chunks.append(PageChunk(
                    text=full, url=url, title=title,
                    section_path=path, chunk_type=kind,
                    chunk_index=start_idx + len(chunks),
                    token_count=t,
                    content_hash=hashlib.md5(full.encode()).hexdigest(),
                ))

        for sent in sentences:
            s_t = self.tokens(sent)
            if buf_t + s_t > self.target and buf:
                emit()
                buf = [buf[-1]] if buf else []   # 1-sentence overlap
                buf_t = self.tokens(buf[0]) if buf else 0
            buf.append(sent)
            buf_t += s_t

        if buf:
            emit()

        return chunks


# ── Helpers ──────────────────────────────────────────────────────────

def _path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(t for _, t in stack)


def _table_to_text(table: Tag) -> str:
    rows: list[str] = []
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    if headers:
        rows.append(" | ".join(headers))
        rows.append("-" * max(len(rows[0]), 3))
    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _list_to_text(lst: Tag) -> str:
    items: list[str] = []
    for li in lst.find_all(("li", "dt", "dd"), recursive=False):
        text = li.get_text(" ", strip=True)
        if text:
            items.append(f"• {text}")
    return "\n".join(items)
