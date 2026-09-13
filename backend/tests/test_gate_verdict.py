"""Gate verdicts: a chatty reviewer must not be able to ship unreviewed work.

The defect these tests pin down is the one the roadmap names: the pipeline used
to decide "did the reviewer pass this?" by string-matching the last line, so a
reviewer that wrote one friendly sentence after its verdict, or wrapped it in
Markdown, was misread. The JSON block is the fix; the line classifier stays as
the fallback, and the cases below are mostly about the *precedence* between
them — above all that a malformed JSON block falls back instead of inventing a
pass.
"""
from __future__ import annotations

from backend.app.orchestrator.fleet.gate import (
    TOOL_DIGEST_MARKER,
    Verdict,
    classify_gate,
    parse_verdict,
)


class TestJsonVerdictWins:
    def test_clean_json_block(self) -> None:
        output = (
            "Walked every task. Files present, names match.\n\n"
            "LGTM\n\n"
            '```json\n{"verdict": "lgtm", "reason": "all 8 tasks present"}\n```'
        )

        v = parse_verdict(output, "reviewer")

        assert v.value == "lgtm"
        assert v.source == "json"
        assert v.reason == "all 8 tasks present"

    def test_json_block_followed_by_trailing_prose(self) -> None:
        """A model that keeps talking after its verdict block. The last-line
        parse is exactly what this used to break."""
        output = (
            "NACK: task 6 missing\n\n"
            '```json\n{"verdict": "nack", "reason": "src/cli.py was never created"}\n```\n\n'
            "Happy to re-review once the coder adds it!"
        )

        v = parse_verdict(output, "reviewer")

        assert v.value == "nack"
        assert v.source == "json"
        assert v.reason == "src/cli.py was never created"

    def test_verdict_spelled_tests_ok_normalizes_to_lgtm(self) -> None:
        output = '```json\n{"verdict": "TESTS_OK", "summary": "4 tests, all green"}\n```'

        v = parse_verdict(output, "tester")

        assert v.value == "lgtm"
        assert v.source == "json"
        # ``summary`` is accepted as the reason when ``reason`` is absent.
        assert v.reason == "4 tests, all green"

    def test_bare_json_object_is_read_when_the_classifier_line_agrees(self) -> None:
        """An unfenced object is only trusted with a second opinion — see
        ``TestAnUnfencedObjectCannotOverruleARejection``."""
        output = 'Tests all pass.\n\nLGTM\n\n{"verdict": "lgtm", "reason": "green"}'

        v = parse_verdict(output, "tester")

        assert (v.value, v.source) == ("lgtm", "json")
        assert v.reason == "green"

    def test_the_last_json_block_wins_over_an_earlier_example(self) -> None:
        """Gate prompts contain an example block; the verdict is the final one."""
        output = (
            'For reference the protocol is ```json\n{"verdict": "lgtm"}\n```\n'
            "but task 3 is missing.\n\n"
            'NACK: task 3 missing\n\n```json\n{"verdict": "nack", "reason": "task 3"}\n```'
        )

        assert parse_verdict(output, "reviewer").value == "nack"

    def test_a_brace_inside_the_reason_does_not_truncate_the_object(self) -> None:
        output = '```json\n{"verdict": "nack", "reason": "missing } in parser.py"}\n```'

        v = parse_verdict(output, "reviewer")

        assert (v.value, v.source) == ("nack", "json")
        assert v.reason == "missing } in parser.py"

    def test_a_multiline_reason_is_collapsed_to_one_line(self) -> None:
        output = '```json\n{"verdict": "nack", "reason": "line one\\nline two"}\n```'

        assert parse_verdict(output, "reviewer").reason == "line one line two"

    def test_an_enormous_reason_is_capped(self) -> None:
        """The structured field must not become a new way to smuggle an
        unbounded payload into the orchestrator's routing data."""
        output = '```json\n{"verdict": "nack", "reason": "%s"}\n```' % ("x" * 5000)

        assert len(parse_verdict(output, "reviewer").reason) <= 500


class TestAnUnfencedObjectCannotOverruleARejection:
    """A fenced ```json block is a deliberate act by the model. A bare
    ``{...}`` in prose is not — it is as likely to be a quotation (a reviewer
    reviewing gate code, quoting its own prompt's example, or pasting a
    fixture) as a verdict. So an unfenced object never gets to turn a
    rejection into a pass.
    """

    def test_a_quoted_verdict_object_does_not_overrule_a_nack_line(self) -> None:
        output = (
            'The test expects {"verdict": "lgtm", "reason": "all 8 present"} here\n'
            "but task 3 is absent.\n\nNACK: task 3 missing"
        )

        v = parse_verdict(output, "reviewer")

        assert v.value == "nack"
        # The audit trail shows the override: the line decided this.
        assert v.source == "line"
        assert classify_gate(output, "reviewer") == "nack"

    def test_an_unfenced_rejection_survives_an_lgtm_line(self) -> None:
        """Fail-safe in the other direction too: whichever side rejects, the
        rejection stands. An ambiguous candidate may not create a pass, and it
        may not be ignored into one either."""
        output = '{"verdict": "nack", "reason": "task 3 missing"}\n\nLGTM'

        assert parse_verdict(output, "reviewer").value == "nack"

    def test_an_unfenced_pass_with_no_classifier_line_fails_safe(self) -> None:
        output = 'Tests all pass.\n\n{"verdict": "lgtm", "reason": "green"}'

        v = parse_verdict(output, "tester")

        assert v.value == "nack_code"
        assert v.source == "line"

    def test_a_fenced_block_still_outranks_the_classifier_line(self) -> None:
        """The hardening must not go too far: a model that deliberately fenced
        its verdict is believed, even against a stale line above it."""
        output = (
            "Task 3 looked missing at first.\n\nNACK: task 3 missing\n\n"
            '```json\n{"verdict": "lgtm", "reason": "task 3 is in helpers.py"}\n```'
        )

        v = parse_verdict(output, "reviewer")

        assert (v.value, v.source) == ("lgtm", "json")
        assert v.reason == "task 3 is in helpers.py"

    def test_two_rejections_that_differ_defer_to_the_line(self) -> None:
        """Both say "no", so nothing ships either way; the unambiguous parser
        picks which kind of no it was."""
        output = (
            '{"verdict": "nack_tests", "reason": "my fixture"}\n\n'
            "NACK_CODE: the scraper returns an empty list"
        )

        v = parse_verdict(output, "tester")

        assert (v.value, v.source) == ("nack_code", "line")


class TestFallsBackToTheLineParser:
    def test_malformed_json_block_falls_back_instead_of_inventing_a_pass(self) -> None:
        """The headline failure mode: broken JSON must never read as LGTM."""
        output = (
            "Task 6 is missing.\n\n"
            "NACK: task 6 unimplemented\n\n"
            '```json\n{"verdict": "nack", "reason": unquoted, trailing,}\n```'
        )

        v = parse_verdict(output, "reviewer")

        assert v.value == "nack"
        assert v.source == "line"

    def test_malformed_json_over_an_lgtm_line_keeps_the_lgtm(self) -> None:
        """Symmetric guard: the fallback must not discard a real verdict
        either, or a broken block would silently NACK good work forever."""
        output = "All tasks present.\n\nLGTM\n\n```json\n{verdict: lgtm\n```"

        v = parse_verdict(output, "reviewer")

        assert (v.value, v.source) == ("lgtm", "line")

    def test_unknown_json_verdict_value_falls_back(self) -> None:
        output = (
            "Mostly fine but task 4 drifted.\n\n"
            "NACK: task 4 renamed the helper\n\n"
            '```json\n{"verdict": "approved_with_comments", "reason": "nit"}\n```'
        )

        v = parse_verdict(output, "reviewer")

        assert v.value == "nack"
        assert v.source == "line"

    def test_json_object_without_a_verdict_key_falls_back(self) -> None:
        output = 'LGTM\n\n```json\n{"files_checked": 8}\n```'

        v = parse_verdict(output, "reviewer")

        assert (v.value, v.source) == ("lgtm", "line")

    def test_line_only_markdown_decorated_verdict(self) -> None:
        v = parse_verdict("Checked everything.\n\n**LGTM**", "reviewer")

        assert (v.value, v.source) == ("lgtm", "line")
        assert v.reason == "**LGTM**"

    def test_chatty_reviewer(self) -> None:
        v = parse_verdict("LGTM\nThanks for the review!", "reviewer")

        assert (v.value, v.source) == ("lgtm", "line")
        assert v.reason == "LGTM"

    def test_tool_digest_after_the_verdict_is_ignored(self) -> None:
        output = (
            "LGTM"
            + TOOL_DIGEST_MARKER
            + 'claude:sonnet)\n- Write input={"verdict": "nack"}\n    [OK] wrote file'
        )

        v = parse_verdict(output, "reviewer")

        # Neither the digest's JSON-looking tool input nor its last line can
        # override the gate's own verdict.
        assert (v.value, v.source) == ("lgtm", "line")


class TestFailSafe:
    def test_nothing_at_all_is_nack_for_a_reviewer(self) -> None:
        v = parse_verdict("I had a look and it seems fine to me.", "reviewer")

        assert v.value == "nack"
        assert v.source == "line"
        assert v.reason == "no verdict found — failing safe"

    def test_nothing_at_all_is_nack_code_for_a_tester(self) -> None:
        assert parse_verdict("ran some tests", "tester").value == "nack_code"

    def test_empty_output_is_nack(self) -> None:
        assert parse_verdict("", "reviewer").value == "nack"

    def test_a_missing_role_still_fails_safe(self) -> None:
        """``role`` is optional on the envelope path; None must not crash and
        must not pass."""
        assert parse_verdict("no idea", None).value == "nack"


class TestTesterValues:
    def test_nack_tests_from_json(self) -> None:
        output = '```json\n{"verdict": "nack_tests", "reason": "my fixture was wrong"}\n```'

        assert parse_verdict(output, "tester").value == "nack_tests"

    def test_nack_tests_from_the_line_parser(self) -> None:
        output = "Tests:\n- test_x — FAIL\n\nNACK_TESTS: bad fixture"

        v = parse_verdict(output, "tester")

        assert (v.value, v.source) == ("nack_tests", "line")


class TestVerdictShape:
    def test_to_dict_is_the_envelope_payload(self) -> None:
        assert Verdict(value="lgtm", reason="why", source="json").to_dict() == {
            "value": "lgtm",
            "reason": "why",
            "source": "json",
        }

    def test_classify_gate_is_unchanged_by_the_json_path(self) -> None:
        """The fallback is only trustworthy if it still behaves exactly as it
        did — including ignoring a JSON block it was never taught to read."""
        output = 'LGTM\n\n```json\n{"verdict": "nack"}\n```'

        assert classify_gate(output, "reviewer") == "lgtm"
        # ...while parse_verdict prefers the machine-readable disagreement.
        assert parse_verdict(output, "reviewer").value == "nack"
