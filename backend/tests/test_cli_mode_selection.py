from __future__ import annotations

from backend.app.core.cli import build_parser, choose_mode


def test_default_preserves_rpc_during_rust_migration() -> None:
    args = build_parser().parse_args([])
    assert choose_mode(args, interactive=True) == 'rpc'
    assert choose_mode(args, interactive=False) == 'rpc'


def test_explicit_modes_and_print_take_precedence() -> None:
    parser = build_parser()
    assert choose_mode(parser.parse_args(['--mode', 'rpc']), interactive=True) == 'rpc'
    assert choose_mode(parser.parse_args(['-p', 'hello']), interactive=True) == 'print'
    assert choose_mode(parser.parse_args(['hello']), interactive=True) == 'print'


def test_explicit_json_preserves_legacy_precedence_over_print() -> None:
    args = build_parser().parse_args(['--mode', 'json', '-p', 'hello'])
    assert choose_mode(args, interactive=True) == 'json'
