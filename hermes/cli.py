"""Thin CLI for kicking the tires on the agent loop.

  uv run python -m hermes.cli --mode native "Find any client named Marshall"
  uv run python -m hermes.cli --mode json   "Draft an intake email for the last call"
  uv run python -m hermes.cli --init-db
  uv run python -m hermes.cli --seed
"""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hermes.agent import AgentMode, run_agent
from hermes.db import init_db

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    parser.add_argument("prompt", nargs="?", default=None, help="user message for the agent")
    parser.add_argument("--mode", choices=["native", "json"], default="native")
    parser.add_argument("--max-iters", type=int, default=8)
    parser.add_argument("--init-db", action="store_true", help="create SQLite schema and exit")
    parser.add_argument("--seed", action="store_true", help="seed Oak & Partners fixtures and exit")
    parser.add_argument("--verbose", action="store_true", help="print full message trace")
    args = parser.parse_args()

    if args.init_db:
        init_db()
        console.print("[green]DB initialized[/green]")
        return 0

    if args.seed:
        from scripts.seed_fake_data import seed

        init_db()
        seed()
        console.print("[green]Seed complete[/green]")
        return 0

    if not args.prompt:
        parser.print_help()
        return 2

    init_db()

    mode = AgentMode(args.mode)
    console.print(Panel.fit(args.prompt, title=f"agent mode={mode.value}", border_style="cyan"))
    run = run_agent(args.prompt, mode=mode, max_iters=args.max_iters)

    tbl = Table(show_header=False, box=None)
    tbl.add_row("session", run.session_id)
    tbl.add_row("halt_reason", run.halt_reason.value)
    tbl.add_row("iterations", str(run.iterations))
    tbl.add_row("llm_sources", ",".join(run.llm_sources) or "-")
    tbl.add_row("approvals_queued", ",".join(str(x) for x in run.approvals_queued) or "-")
    console.print(tbl)

    console.print(Panel(run.final_text or "(empty)", title="final", border_style="green"))

    if args.verbose:
        console.rule("messages")
        console.print_json(data=run.messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
