"""Chunking tests — the evidence behind the DSN "failure I fixed" story.

`test_naive_chunking_severs_the_pdt_rule` and `test_heading_chunking_keeps_the_rule_whole` are the
before/after. They run against the real corpus doc, so the demo claim is backed by an executing
test rather than a remembered anecdote.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.rag.chunking import Chunk, chunk_corpus, fixed_size_chunks, heading_chunks

CORPUS = Path(__file__).resolve().parents[2] / "corpus"
PDT_DOC = CORPUS / "pattern-day-trader-rule.md"


@pytest.fixture(scope="module")
def pdt_text() -> str:
    return PDT_DOC.read_text(encoding="utf-8")


def _containing(chunks: list[Chunk], needle: str) -> list[Chunk]:
    return [c for c in chunks if needle in c.body]


class TestTheFailureIFixed:
    """The demo's centerpiece, as executable assertions."""

    def test_naive_chunking_severs_the_pdt_rule(self, pdt_text: str) -> None:
        """The bug: the $25,000 requirement and the consequence of breaching it land in
        different chunks, so retrieving one gives the model half a rule to complete."""
        chunks = fixed_size_chunks(
            pdt_text, source="pattern-day-trader-rule.md", size=500, overlap=50
        )

        requirement = _containing(chunks, "$25,000 in account equity")
        consequence = _containing(chunks, "day trading is prohibited")

        assert requirement, "fixture drift: the requirement text is gone from the corpus doc"
        assert consequence, "fixture drift: the consequence text is gone from the corpus doc"

        # The actual defect: no single naive chunk holds both halves.
        both = set(requirement) & set(consequence)
        assert not both, (
            "Naive chunking happened to keep the rule together — the demo's before/after no "
            "longer demonstrates anything. Re-tune size/overlap or pick another example."
        )

    def test_heading_chunking_keeps_the_rule_whole(self, pdt_text: str) -> None:
        """The fix: the equity requirement lives in one coherent section."""
        chunks = heading_chunks(pdt_text, source="pattern-day-trader-rule.md")
        requirement = _containing(chunks, "$25,000 in account equity")

        assert len(requirement) == 1, "the requirement should live in exactly one section"
        chunk = requirement[0]

        # It carries the context needed to interpret it standalone.
        assert "$25,000" in chunk.body
        assert "before" in chunk.body.lower(), "the timing condition must survive with the number"
        assert "equity" in chunk.heading_path[-1].lower()

    def test_naive_chunks_lose_the_document_title(self, pdt_text: str) -> None:
        """Every naive chunk claims the same flat path, so retrieval can't tell the reader which
        rule a fragment belongs to."""
        chunks = fixed_size_chunks(pdt_text, source="pattern-day-trader-rule.md")
        paths = {c.heading_path for c in chunks}
        assert len(paths) == 1, "naive chunking has no heading structure to preserve"


class TestTitlePrefixing:
    def test_embedding_text_includes_the_heading_path(self, pdt_text: str) -> None:
        """A user asking 'what is the PDT rule?' uses words in the heading, not necessarily in
        the paragraph. Embedding the bare body makes the chunk hard to find."""
        chunk = _containing(heading_chunks(pdt_text, source="pdt.md"), "$25,000 in account equity")[
            0
        ]
        embedded = chunk.to_embedding_text()
        assert "Pattern Day Trader" in embedded
        assert "$25,000" in embedded

    def test_citation_names_the_section(self, pdt_text: str) -> None:
        chunk = _containing(heading_chunks(pdt_text, source="pdt.md"), "$25,000 in account equity")[
            0
        ]
        assert "Pattern Day Trader" in chunk.citation
        assert "—" in chunk.citation


class TestHeadingStructure:
    def test_nested_headings_produce_an_ancestor_path(self) -> None:
        text = (
            "# Doc\n\n## Section\n\nSome body text that is definitely long enough to keep.\n"
            "\n### Sub\n\nAnother body long enough to survive the minimum length filter.\n"
        )
        chunks = heading_chunks(text, source="d.md")
        sub = [c for c in chunks if c.heading_path[-1] == "Sub"][0]
        assert sub.heading_path == ("Doc", "Section", "Sub")

    def test_sibling_headings_do_not_nest(self) -> None:
        text = (
            "# Doc\n\n## A\n\nBody for section A that is long enough to be kept as a chunk.\n"
            "\n## B\n\nBody for section B that is long enough to be kept as a chunk.\n"
        )
        chunks = heading_chunks(text, source="d.md")
        b = [c for c in chunks if c.heading_path[-1] == "B"][0]
        assert b.heading_path == ("Doc", "B"), "B is a sibling of A, not its child"

    def test_heading_with_no_body_is_not_a_chunk(self) -> None:
        text = (
            "# Doc\n\n## Empty\n\n## Real\n\nThis section has a body long enough to be retained.\n"
        )
        titles = [c.heading_path[-1] for c in heading_chunks(text, source="d.md")]
        assert "Empty" not in titles
        assert "Real" in titles

    def test_body_excludes_its_own_heading_line(self) -> None:
        text = "# Doc\n\n## Heading Text Here\n\nThe body content, long enough to be kept around.\n"
        chunk = heading_chunks(text, source="d.md")[0]
        assert not chunk.body.startswith("#")
        assert "## Heading Text Here" not in chunk.body

    def test_document_without_headings_becomes_one_chunk(self) -> None:
        text = "Just a paragraph of prose with no headings at all, long enough to be retained."
        chunks = heading_chunks(text, source="plain-doc.md")
        assert len(chunks) == 1
        assert chunks[0].doc_title == "Plain Doc"


class TestCorpus:
    def test_corpus_chunks_cleanly(self) -> None:
        chunks = chunk_corpus(CORPUS)
        assert chunks
        assert all(c.body.strip() for c in chunks), "no empty chunks — they match everything weakly"

    def test_readme_is_not_indexed(self) -> None:
        """corpus/README.md documents authoring rules for humans. Indexed, it would answer user
        questions with 'One concept per ## heading', which is nonsense to a trader."""
        assert all(c.source.lower() != "readme.md" for c in chunk_corpus(CORPUS))

    def test_every_chunk_can_be_cited(self) -> None:
        """An answer the user can't trace back to a document is an answer they must take on
        faith — which is the thing RAG exists to avoid."""
        for chunk in chunk_corpus(CORPUS):
            assert chunk.citation.strip()
            assert chunk.source.endswith(".md")

    def test_empty_corpus_fails_loudly(self, tmp_path: Path) -> None:
        """Silently indexing nothing means every answer becomes 'not in my docs' with no clue
        why. Fail at build time instead."""
        with pytest.raises(ValueError, match="no chunks"):
            chunk_corpus(tmp_path)

    def test_missing_corpus_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            chunk_corpus(tmp_path / "nope")
