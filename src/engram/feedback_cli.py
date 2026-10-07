"""CLI bridge for MCP services that do not embed the Python adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engram.feedback import DEFAULT_MCP_OBSERVABILITY_PATH, MCPObservability


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-feedback")
    parser.add_argument(
        "--path", type=Path, default=DEFAULT_MCP_OBSERVABILITY_PATH
    )
    parser.add_argument("--service", required=True)
    parser.add_argument("--instance", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    event = sub.add_parser("event")
    event.add_argument("--tool", required=True)
    event.add_argument("--variant", default="default")
    event.add_argument("--ok", action="store_true")
    event.add_argument("--latency-ms", type=int, default=0)
    event.add_argument("--error-type", default="")
    event.add_argument("--request-id", default="")
    add = sub.add_parser("add")
    add.add_argument("--summary", required=True)
    add.add_argument("--category", default="other")
    add.add_argument("--severity", default="medium")
    add.add_argument("--expected", default="")
    add.add_argument("--actual", default="")
    add.add_argument("--tool", default="")
    report = sub.add_parser("report")
    report.add_argument("--window-days", type=int, default=14)
    report.add_argument("--limit", type=int, default=10)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    observer = MCPObservability(
        args.service,
        instance=args.instance,
        path=args.path,
    )
    if args.command == "event":
        payload = {
            "recorded": observer.record_event(
                tool=args.tool,
                variant=args.variant,
                ok=args.ok,
                latency_ms=args.latency_ms,
                error_type=args.error_type,
                request_id=args.request_id,
            )
        }
    elif args.command == "add":
        payload = observer.submit_feedback(
            summary=args.summary,
            category=args.category,
            severity=args.severity,
            expected=args.expected,
            actual=args.actual,
            tool=args.tool,
        )
    else:
        payload = observer.report(
            window_days=args.window_days,
            limit=args.limit,
        )
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
