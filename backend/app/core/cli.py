"""``localcode`` — pi's run modes over official engines.

    localcode --mode rpc   [--engine claude|codex|fake] [--model M] [--cwd DIR]
    localcode --mode json  "prompt"
    localcode -p           "prompt"        # print mode
    localcode --session continue|new|/path/to/session.jsonl
    localcode --trust-project -e ./ext.py

Interactive use is the web UI (or any RPC client); there is no TUI here.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from .agent_session import create_agent_session
from .engines import create_engine
from .modes import run_json_mode, run_print_mode
from .rpc.server import run_rpc_mode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="localcode", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("prompt", nargs="?", help="prompt for -p / --mode json (or read from stdin)")
    p.add_argument("--mode", choices=["rpc", "json"], default=None)
    p.add_argument(
        "-p",
        "--print",
        action="store_true",
        dest="print_mode",
        help="run one prompt and print the answer",
    )
    p.add_argument("--engine", default=os.environ.get("LOCALCODE_ENGINE", "claude"))
    p.add_argument("--model", default=os.environ.get("LOCALCODE_MODEL"))
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--session", default="new", help="new | continue | <path.jsonl>")
    p.add_argument("--thinking", default="off")
    p.add_argument("--permission-mode", default=None, help="claude permission_mode / codex sandbox")
    p.add_argument("--default-permission", choices=["ask", "allow", "deny"], default="ask")
    p.add_argument("--append-system-prompt", default=None)
    p.add_argument("--trust-project", action="store_true", help="load <cwd>/.localcode extensions")
    p.add_argument("-e", "--extension", action="append", default=[], help="extension file or dir")
    p.add_argument("--no-extensions", action="store_true")
    p.add_argument("--in-memory", action="store_true", help="do not persist the session")
    p.add_argument(
        "--models",
        default=os.environ.get("LOCALCODE_MODELS", ""),
        help="comma-separated provider/model list for the picker",
    )
    return p


def _models(spec: str) -> list[dict[str, str]]:
    out = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        provider, _, model_id = raw.partition("/")
        if model_id:
            out.append({"provider": provider, "modelId": model_id})
    return out


async def _main(args: argparse.Namespace) -> int:
    engine = create_engine(args.engine)
    mode = "rpc" if args.mode == "rpc" else ("json" if args.mode == "json" else "print")
    session = await create_agent_session(
        engine=engine,
        cwd=args.cwd,
        session=args.session,
        in_memory=args.in_memory,
        extension_paths=args.extension,
        discover_extensions=not args.no_extensions,
        project_trusted=args.trust_project,
        mode=mode,
        model=args.model,
        thinking_level=args.thinking,
        append_system_prompt=args.append_system_prompt,
        permission_mode=args.permission_mode,
        default_permission=args.default_permission,
    )
    if mode == "rpc":
        await run_rpc_mode(session, models=_models(args.models))
        return 0
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        sys.stderr.write("no prompt given\n")
        return 2
    if mode == "json":
        return await run_json_mode(session, prompt)
    return await run_print_mode(session, prompt)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.cwd = os.path.abspath(args.cwd)
    if not args.mode and not args.print_mode:
        args.mode = "rpc" if args.prompt is None else None
        if args.mode is None:
            args.print_mode = True
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:  # console_scripts target
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()


__all__: list[Any] = ["main", "build_parser"]
