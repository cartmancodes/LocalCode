"""The artifact store: a 2 MB output must never land in the model's context.

Also covers the two guarantees Task 6 (the fleet step collector) is written
against: ``store_if_large``'s exact return shape, and that identical content
dedupes to one file rather than accumulating copies.
"""
from __future__ import annotations

from pathlib import Path

from backend.app.artifacts import ArtifactRef, ArtifactStore, default_artifact_root


class TestContentAddressing:
    def test_same_content_stored_twice_yields_one_file_and_the_same_id(
        self, tmp_path: Path
    ) -> None:
        store = ArtifactStore(root=tmp_path)

        ref1 = store.put_text("hello world", kind="tool-result")
        ref2 = store.put_text("hello world", kind="tool-result")

        assert ref1.id == ref2.id
        assert ref1.path == ref2.path
        files = list(tmp_path.rglob("*.txt"))
        assert len(files) == 1

    def test_get_text_round_trips(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)
        ref = store.put_text("some content", kind="step-output")

        assert store.get_text(ref.id) == "some content"

    def test_get_text_for_unknown_id_is_none(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)
        assert store.get_text("0" * 64) is None

    def test_the_default_root_resolves_under_a_redirected_home(
        self, tmp_localcode: Path, fresh_settings
    ) -> None:
        """The bug Task 13 hit: a module-level ``Path.home()`` freezes before
        a test's HOME redirect takes effect. The default must be computed
        when the store (or the bare helper) is actually asked for it."""
        assert default_artifact_root() == tmp_localcode / ".localcode" / "artifacts"

        store = ArtifactStore()
        assert store.root == tmp_localcode / ".localcode" / "artifacts"

        ref = store.put_text("x", kind="tool-result")
        assert tmp_localcode in ref.path.parents


class TestSummarizeForContext:
    def test_under_the_threshold_is_returned_unchanged(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)
        text = "short output"
        ref = store.put_text(text, kind="tool-result")

        assert store.summarize_for_context(text, ref, max_bytes=1000) == text

    def test_over_the_threshold_is_bounded_and_carries_the_path_and_id(
        self, tmp_path: Path
    ) -> None:
        store = ArtifactStore(root=tmp_path)
        text = "A" * 100 + "B" * 200_000 + "Z" * 100
        ref = store.put_text(text, kind="tool-result")
        max_bytes = 3_000

        summary = store.summarize_for_context(text, ref, max_bytes=max_bytes)

        marker_bytes = len(
            f"\n… [truncated {len(text.encode())} bytes — full output at "
            f"{ref.path} (artifact {ref.id[:12]})]\n".encode()
        )
        assert len(summary.encode("utf-8")) <= max_bytes + marker_bytes
        assert str(ref.path) in summary
        assert ref.id[:12] in summary
        assert summary.startswith("A" * 50)
        assert summary.endswith("Z" * 50)

    def test_never_splits_a_multibyte_character(self, tmp_path: Path) -> None:
        """The trap: slicing on raw bytes (not characters) can cut a UTF-8
        sequence in half and either raise on decode or produce mojibake."""
        store = ArtifactStore(root=tmp_path)
        # CJK (3 bytes/char) and emoji (4 bytes/char) padding around a huge
        # ASCII body, sized so a naive byte-offset slice lands mid-character.
        text = "漢" * 5000 + "x" * 100_000 + "🎉" * 5000
        ref = store.put_text(text, kind="tool-result")

        summary = store.summarize_for_context(text, ref, max_bytes=1001)

        # Must round-trip cleanly — a split multi-byte sequence would raise
        # here or (if it happened to decode) not equal the original slice.
        summary.encode("utf-8").decode("utf-8")
        assert "�" not in summary  # no replacement-character mojibake


class TestStoreIfLarge:
    def test_a_two_megabyte_string_round_trips(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)
        big = "line of output\n" * 140_000  # a bit over 2 MB
        assert len(big.encode("utf-8")) > 2_000_000

        summary, ref = store.store_if_large(big, kind="step-output", max_bytes=8_000)

        assert ref is not None
        assert isinstance(ref, ArtifactRef)
        assert len(summary.encode("utf-8")) < len(big.encode("utf-8"))
        assert store.get_text(ref.id) == big

    def test_small_text_is_not_stored(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)

        summary, ref = store.store_if_large("tiny", kind="step-output", max_bytes=8_000)

        assert summary == "tiny"
        assert ref is None
        assert list(tmp_path.rglob("*.txt")) == []


class TestAtomicWrites:
    def test_a_write_leaves_no_tmp_file_behind(self, tmp_path: Path) -> None:
        store = ArtifactStore(root=tmp_path)
        ref = store.put_text("content", kind="tool-result")

        assert ref.path.exists()
        assert list(ref.path.parent.glob("*.tmp")) == []
