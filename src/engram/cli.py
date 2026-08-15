from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from engram.config import load_config
from engram.db import connect
from engram.domain import RecordDraft
from engram.errors import EngramError, exit_code_for
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService


def _emit(payload: dict[str, object], *, human: bool) -> None:
    if human:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--human", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record").add_subparsers(
        dest="record_command", required=True
    )
    create = record.add_parser("create")
    create.add_argument("--title", default="")
    create.add_argument("--body", required=True)
    create.add_argument("--type", dest="record_type", default="note")
    create.add_argument("--project", action="append", default=[])
    create.add_argument("--agent", default="unknown")

    get = record.add_parser("get")
    get.add_argument("record_id")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", default="keyword", choices=["keyword"])
    search.add_argument("--top-k", type=int, default=5)

    sub.add_parser("status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = load_config(data_dir=args.data_dir)
    connection = connect(config.db_path)
    migrate(connection)
    repository = RecordRepository(connection)
    search = SearchService(connection)
    instance = f"cli:{args.command}"
    try:
        if args.command == "record" and args.record_command == "create":
            record = repository.create(
                RecordDraft(
                    title=args.title,
                    body=args.body,
                    record_type=args.record_type,
                    projects=tuple(args.project),
                    source_agent=args.agent,
                )
            )
            payload = record.to_dict()
            payload["backlog"] = repository.backlog()
            _emit(payload, human=args.human)
            return 0
        if args.command == "record" and args.record_command == "get":
            _emit(repository.get(args.record_id).to_dict(), human=args.human)
            return 0
        if args.command == "search":
            hits = search.keyword(args.query, limit=args.top_k)
            _emit({"results": [hit.to_dict() for hit in hits]}, human=args.human)
            return 0
        if args.command == "status":
            _emit(
                {
                    "records": repository.count(),
                    "backlog": repository.backlog(),
                    "data_dir": str(config.data_dir),
                },
                human=args.human,
            )
            return 0
    except EngramError as error:
        problem = error.to_problem(instance=instance).to_dict()
        print(
            json.dumps(
                problem, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ),
            file=sys.stderr,
        )
        return exit_code_for(error)
    return 64
