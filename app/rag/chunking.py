"""Heading-based chunking with title prefixing.

## This module is the DSN demo's "failure I fixed"

Naive fixed-size chunking splits on a character count with no regard for meaning. On
`corpus/pattern-day-trader-rule.md` it cut mid-rule: one chunk ended with "...must maintain at
least $25,000 in account equity" and the next began "...if a flagged account drops below". A
question about the equity minimum retrieved a fragment, and the model completed the missing half
from its own priors — producing an answer that sounded authoritative and was wrong.

Confident wrong answers are worse than "I don't know", because the user has no way to detect them.

The fix has two parts, and both matter:

1. **Split on headings, never mid-section.** A `##` section is authored to be one self-contained
   concept, so it survives being read in isolation — which is exactly how retrieval presents it.
2. **Prefix every chunk with its document title and heading path.** A chunk that reads
   "$25,000 in account equity" is ambiguous out of context. Prefixed with
   "Pattern Day Trader (PDT) Rule > The $25,000 minimum equity requirement", it is not. This also
   materially improves embedding quality: the title terms are in the vector.

`fixed_size_chunks` is kept deliberately, so the demo can show before/after live rather than
describe it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: A markdown ATX heading: captures level and text.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit."""

    #: Document title (the `# H1`, or the filename if absent).
    doc_title: str
    #: Heading path from H1 down to this section, e.g. ["PDT Rule", "The $25,000 minimum"].
    heading_path: tuple[str, ...]
    #: The section body, without the heading line.
    body: str
    source: str

    @property
    def citation(self) -> str:
        """What the UI shows so the user can check the answer against the doc."""
        if len(self.heading_path) > 1:
            return f"{self.doc_title} — {self.heading_path[-1]}"
        return self.doc_title

    def to_embedding_text(self) -> str:
        """The text that actually gets embedded and returned to the model.

        The heading path is included on purpose. Embedding the bare body loses the terms that
        make the chunk findable: a user asking "what is the PDT rule?" uses words that appear in
        the *heading*, not necessarily in the paragraph beneath it.
        """
        path = " > ".join(self.heading_path)
        return f"{path}\n\n{self.body}".strip()

    def __len__(self) -> int:
        return len(self.body)


def _title_of(text: str, fallback: str) -> str:
    for match in _HEADING.finditer(text):
        if len(match.group(1)) == 1:
            return match.group(2).strip()
    return fallback


def heading_chunks(text: str, *, source: str, min_chars: int = 40) -> list[Chunk]:
    """Split markdown into one chunk per heading section.

    Sections shorter than `min_chars` are dropped — they're almost always a heading with no body
    yet, and an empty chunk pollutes retrieval by matching everything weakly.
    """
    doc_title = _title_of(text, fallback=Path(source).stem.replace("-", " ").title())

    matches = list(_HEADING.finditer(text))
    if not matches:
        body = text.strip()
        if len(body) < min_chars:
            return []
        return [Chunk(doc_title=doc_title, heading_path=(doc_title,), body=body, source=source)]

    chunks: list[Chunk] = []
    # Tracks the current heading at each level so a section knows its ancestors.
    path_stack: list[tuple[int, str]] = []

    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()

        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        # Pop any headings at this level or deeper: they are siblings/children, not ancestors.
        while path_stack and path_stack[-1][0] >= level:
            path_stack.pop()
        path_stack.append((level, heading))

        if len(body) < min_chars:
            # A heading whose body is short (or which only introduces subsections) still belongs
            # in the path of its children — it just isn't a chunk of its own.
            continue

        chunks.append(
            Chunk(
                doc_title=doc_title,
                heading_path=tuple(h for _, h in path_stack),
                body=body,
                source=source,
            )
        )

    return chunks


def fixed_size_chunks(text: str, *, source: str, size: int = 500, overlap: int = 50) -> list[Chunk]:
    """Naive character-count chunking. **The bug, preserved for the demo.**

    Kept so the DSN talk can show the before/after side by side instead of asserting it. Do not
    use this for the real index — see `test_chunking.py`, which demonstrates it severing the PDT
    equity rule from its consequence.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be in [0, size)")

    doc_title = _title_of(text, fallback=Path(source).stem)
    body = text.strip()
    chunks: list[Chunk] = []
    start = 0
    step = size - overlap

    while start < len(body):
        chunks.append(
            Chunk(
                doc_title=doc_title,
                heading_path=(doc_title,),
                body=body[start : start + size],
                source=source,
            )
        )
        start += step

    return chunks


def chunk_corpus(corpus_dir: Path | str) -> list[Chunk]:
    """Chunk every markdown doc in the corpus.

    Skips README.md — it documents the corpus for humans and would otherwise answer user
    questions with authoring guidelines.
    """
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.exists():
        raise FileNotFoundError(f"no corpus at {corpus_dir}")

    chunks: list[Chunk] = []
    for path in sorted(corpus_dir.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        chunks.extend(heading_chunks(path.read_text(encoding="utf-8"), source=path.name))

    if not chunks:
        raise ValueError(
            f"{corpus_dir} produced no chunks. The agent would answer every question with "
            f"'not in my docs' and nothing would explain why."
        )
    return chunks
