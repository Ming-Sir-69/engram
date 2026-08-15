from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from engram.config import load_config
from engram.db import connect
from engram.domain import RecordDraft
from engram.errors import EngramError, exit_code_for
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService
from engram.sync import sync_derived


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
    search.add_argument(
        "--mode", default="keyword", choices=["keyword", "vector", "hybrid"]
    )
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--offline", action="store_true")

    index = sub.add_parser("index").add_subparsers(dest="index_command", required=True)
    drain = index.add_parser("drain")
    drain.add_argument("--limit", type=int, default=20)
    drain.add_argument("--offline", action="store_true")

    migrate_command = sub.add_parser("migrate").add_subparsers(
        dest="migrate_command", required=True
    )
    from_markdown = migrate_command.add_parser("from-markdown")
    from_markdown.add_argument("--source", default=None)
    from_markdown.add_argument("--taxonomy", default=None)
    from_markdown.add_argument("--dry-run", action="store_true")

    export = sub.add_parser("export").add_subparsers(
        dest="export_command", required=True
    )
    export_markdown = export.add_parser("markdown")
    export_markdown.add_argument("--out", required=True)
    # 接管人工维护的目录是一次性动作，必须显式说出来：
    # 默认拒绝覆盖非本工具生成的文件，是这里最有价值的一道保护。
    export_markdown.add_argument("--adopt", action="store_true")
    export_jsonl = export.add_parser("jsonl")
    export_jsonl.add_argument("--out", required=True)
    export_index = export.add_parser("index")
    export_index.add_argument("--out", required=True)

    bench = sub.add_parser("bench").add_subparsers(dest="bench_command", required=True)
    recall = bench.add_parser("recall")
    recall.add_argument("--gold", required=True)
    recall.add_argument("--top-k", type=int, default=5)
    recall.add_argument(
        "--mode", default="keyword", choices=["keyword", "vector", "hybrid"]
    )
    recall.add_argument("--offline", action="store_true")
    recall.add_argument("--min-hits", type=int, default=None)

    serve_mcp = sub.add_parser("mcp")
    serve_mcp.add_argument("--offline", action="store_true")

    sub.add_parser("status")
    return parser


def _vector_components(config, *, offline: bool, connection):
    """按需构建向量组件。

    这里刻意使用延迟导入：纯写入路径（record create）不应加载嵌入与
    向量模块，从而在依赖层面保证模型不可用绝不影响写入。
    """
    from engram.embedding import DeterministicEmbedder, OllamaEmbedder
    from engram.vectors import VectorStore

    if offline:
        embedder = DeterministicEmbedder(dimensions=64)
    else:
        embedder = OllamaEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            base_url=config.ollama_base_url,
        )
    store = VectorStore(connection, dimensions=embedder.dimensions)
    return embedder, store


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
            sync = sync_derived(config=config, repository=repository)
            if sync is not None:
                payload["sync"] = sync
            _emit(payload, human=args.human)
            return 0
        if args.command == "record" and args.record_command == "get":
            _emit(repository.get(args.record_id).to_dict(), human=args.human)
            return 0
        if args.command == "index" and args.index_command == "drain":
            from engram.classify import Classifier, OllamaLabelModel
            from engram.enrich import EnrichmentService

            embedder, store = _vector_components(
                config, offline=args.offline, connection=connection
            )
            # 离线模式不接模型，仅用于确定性测试；生产路径必须接上本地
            # 分类器，否则四层降级链缺第三层，冷启动时标签体系无法建立。
            label_model = (
                None
                if args.offline
                else OllamaLabelModel(
                    model=config.classifier_model,
                    base_url=config.ollama_base_url,
                )
            )
            service = EnrichmentService(
                repository=repository,
                store=store,
                embedder=embedder,
                classifier=Classifier(store=store, model=label_model),
                generation=f"{embedder.model}-{embedder.dimensions}",
            )
            _emit(service.drain(limit=args.limit).to_dict(), human=args.human)
            return 0
        if args.command == "migrate" and args.migrate_command == "from-markdown":
            from engram.ingest.migrate import migrate_from_markdown
            from engram.ingest.taxonomy import load_seed_taxonomy

            report = migrate_from_markdown(
                source_dir=Path(args.source) if args.source else config.source_dir,
                repository=repository,
                taxonomy=load_seed_taxonomy(
                    Path(args.taxonomy) if args.taxonomy else config.seed_taxonomy_path
                ),
                dry_run=args.dry_run,
            )
            _emit(report.to_dict(), human=args.human)
            return 0
        if args.command == "export":
            from engram.export import export_index, export_jsonl, export_markdown

            if args.export_command == "markdown":
                exported = export_markdown(
                    repository=repository, out_dir=Path(args.out), adopt=args.adopt
                )
            elif args.export_command == "index":
                exported = export_index(
                    repository=repository, out_file=Path(args.out)
                )
            else:
                exported = export_jsonl(
                    repository=repository, out_file=Path(args.out)
                )
            _emit(exported, human=args.human)
            return 0
        if args.command == "bench" and args.bench_command == "recall":
            from engram.bench import load_gold, run_recall

            embed_query = None
            if args.mode != "keyword":
                embedder, store = _vector_components(
                    config, offline=args.offline, connection=connection
                )
                search.store = store

                def embed_query(text: str) -> list[float]:
                    return embedder.embed([text])[0]

            report = run_recall(
                gold=load_gold(Path(args.gold)),
                search=search,
                top_k=args.top_k,
                mode=args.mode,
                embed_query=embed_query,
            )
            measured = report.to_dict()
            if args.min_hits is None:
                _emit(measured, human=args.human)
                return 0
            # 门槛由工具判定并体现在退出码上，这样它才能挂进脚本和阶段验收，
            # 而不是依赖有人去读那一行分数。
            achieved = report.hits[args.top_k]
            passed = achieved >= args.min_hits
            measured["gate"] = {
                "min_hits": args.min_hits,
                "hits": achieved,
                "passed": passed,
            }
            _emit(measured, human=args.human)
            return 0 if passed else 1
        if args.command == "search":
            if args.mode == "keyword":
                hits = search.keyword(args.query, limit=args.top_k)
            else:
                embedder, store = _vector_components(
                    config, offline=args.offline, connection=connection
                )
                search.store = store
                vector = embedder.embed([args.query])[0]
                hits = (
                    search.vector(vector, limit=args.top_k)
                    if args.mode == "vector"
                    else search.hybrid(args.query, vector, limit=args.top_k)
                )
            _emit({"results": [hit.to_dict() for hit in hits]}, human=args.human)
            return 0
        if args.command == "mcp":
            from engram.mcp.server import serve
            from engram.mcp.tools import ToolContext

            # 复用已建好的连接：stdout 在这条路径上是协议通道，
            # 任何额外输出都会让宿主解析失败，因此这里不打印任何东西。
            serve(
                ToolContext(
                    config=config,
                    repository=repository,
                    search=search,
                    offline=args.offline,
                )
            )
            return 0
        if args.command == "status":
            payload: dict[str, object] = {
                "records": repository.count(),
                "backlog": repository.backlog(),
                "data_dir": str(config.data_dir),
            }
            payload["vectors"] = connection.execute(
                "SELECT COUNT(*) FROM embeddings"
            ).fetchone()[0]
            _emit(payload, human=args.human)
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
