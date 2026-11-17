<picture>
  <source media="(prefers-color-scheme: dark)" srcset="readme-assets/cover-en-dark.png">
  <img alt="Engram" src="readme-assets/cover-en-light.png" width="100%">
</picture>

<p align="center"><a href="docs/architecture.md">Architecture</a> · <a href="docs/research-directions.md">Research</a> · <a href="CHANGELOG.md">Changelog</a> · <a href="CONTRIBUTING.md">Contributing</a></p>
<p align="center">
<img alt="MIT" src="https://img.shields.io/badge/license-MIT-F6C555?style=flat-square&amp;labelColor=2E2A26">
<img alt="Version" src="https://img.shields.io/badge/version-0.4.2-6B655C?style=flat-square&amp;labelColor=2E2A26">
<img alt="Python" src="https://img.shields.io/badge/Python-3.13%2B-6B655C?style=flat-square&amp;labelColor=2E2A26">
<img alt="SQLite" src="https://img.shields.io/badge/storage-SQLite-A8A196?style=flat-square&amp;labelColor=2E2A26">
</p>
<p align="center"><a href="README.md">简体中文</a> · <b>English</b></p>

# Engram · Shared local knowledge for agents

Engram 0.4.2 keeps long-term knowledge in a local SQLite database shared by different agents. Writers provide content; classification and vectors are completed later. Explicit corrections preserve provenance while reads distinguish currently applicable information from original history.

Earlier public code declared 0.1.0 and had no formal release tags. Version 0.4.0 first packaged the current core as public source; 0.4.1 updated CI, documentation, and its publication checks, and 0.4.2 updates the signature and covers. Intermediate development is described as milestones, without inventing 0.2/0.3 releases. See the [changelog](CHANGELOG.md), [architecture](docs/architecture.md), [evolution](docs/evolution.md), and [research directions](docs/research-directions.md).


## Features

- Record content, project bindings, revisions, FTS, and outbox work commit together. ID and keyword reads become available on commit; vectors and curation are separate readiness states.
- Optional `remember.corrections` identifies a target, whole-record or partial scope, and an explicit source. Whole-record replacement uses `supersedes`; partial correction uses `corrects`. Exact partial replacement requires an unchanged baseline, a unique excerpt, and disjoint ranges.
- `recall(view="current")` resolves applicable projections. `history` and `get` retain original content. Old words, vectors, or graph edges can reach current replacements through explicit correction chains.
- Projects are optional, bounded ranking preferences. Records remain searchable without project metadata; age or similarity alone does not invalidate a fact.
- Keyword, vector, and RRF hybrid search; explicit keyword degradation on semantic failure. MLX models load on demand, with serialized inference through a shared loopback embedding service.
- stdio MCP plus optional official SDK HTTP/SSE. Remote access retains Bearer/OAuth authentication, PKCE, resource binding, Host/Origin checks, and owner authorization.
- A Claude Code command Hook can retrieve relevant keyword context and re-prompt after version changes while failing open on errors. This package does not install a personal Hook configuration or claim automatic adoption in every host conversation.

- After vector writes, kNN neighbors form automatic bidirectional links; the link score is `1/(1+distance)`, with scores below 0.5 excluded by default.
- Autonomous maintenance supports record revision, archiving, and restoration within a bounded operation set, with optimistic concurrency checks and rollback; it is disabled by default, and archiving is not time-decay forgetting.

## Quick start

Requires Python 3.13+, uv, and Apple Silicon macOS for the current MLX dependency set. Keyword reads and writes do not load a model.

The vector layer also requires SQLite extension loading (`enable_load_extension`), which some macOS Python distributions omit. The command below uses uv-managed Python; CI checks this capability before running the complete synthetic suite.

```sh
uv sync --managed-python --all-groups --extra remote
uv run python examples/synthetic_corrections.py
uv run pytest
uv run ruff check .
```

The example creates temporary, invented sensor records and shows how an old keyword reaches a corrected value. Its temporary database is removed afterward. Deterministic `--offline` vectors test engineering behavior, not semantic model quality.

A separate library sets `ENGRAM_DATA_DIR` to a directory outside the code checkout. These records are also invented:

```sh
export ENGRAM_DATA_DIR="$HOME/engram-data"
export ENGRAM_USAGE_TELEMETRY=0
uv run engram record create --title "Synthetic sensor" --body "synthetic-prism channel=amber"
uv run engram search "synthetic-prism" --mode keyword
uv run engram --human status
```

## Usage

```sh
uv run engram mcp
```

Standard tools include `remember`, `recall`, `get`, `status`, and an independent `feedback` inbox. The Claude presentation profile shares the same tool contract. Remote exposure requires authentication; `uv run engram mcp --help` lists transport options.

The default embedding model belongs to the Qwen3 Embedding series.<sup>[[3]](#references)</sup>

Real semantic models are prepared separately under `$ENGRAM_DATA_DIR/models/`. Defaults are `Qwen3-Embedding-0.6B` and `Qwen3-1.7B-4bit`. Missing models are never downloaded automatically. `index drain` completes pending work; `index rebuild` stages vectors and checks source consistency before replacing the index.

## Design

SQLite is the fact store; content and full-text indexing commit together, while the semantic layer completes later. Hybrid retrieval uses reciprocal rank fusion (RRF) to combine ranked result lists.<sup>[[1]](#references)</sup>

Vector tables use sqlite-vec `vec0`.<sup>[[2]](#references)</sup>

[Architecture and public-copy differences](docs/architecture.md) · [Evolution](docs/evolution.md)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="readme-assets/write-path-en-dark.svg">
  <img alt="Write and indexing sequence" src="readme-assets/write-path-en-light.svg" width="100%">
</picture>

## Limitations

The package contains owned core source, synthetic tests, examples, and documentation. It excludes actual knowledge, logs, feedback, context, receipts, personal configuration, operations runners, and model weights. Content maintenance, observability, and fixed evidence modules are reusable source; examples disable observation and automatic maintenance. `source_improvement` requires a separately supplied deployment gateway, which is excluded and hidden by default.

The public copy uses a generic authenticator label and explicit `ENGRAM_RUN_SCHEDULES` instead of deployment timings; only a manual entry exists by default. Authentication and access boundaries are retained and strengthened: ASGI prefixes cannot bypass Bearer checks or explicit queue/time limits; timeout-only configuration releases requests safely; initialized icon metadata matches the SVG response; replay of a spent refresh token for the same client/resource revokes its active family. The old token key is hashed, its association retains only the original bounded expiry, and no old plaintext token is stored. These fixes apply to this public copy; the private deployment is unchanged. See [public-copy differences](docs/architecture.md).

Synthetic engineering checks validate contracts, counterexamples, and isolation behavior; they do not establish user benefit, long-term recall quality, or host adoption rates. Results reported by research papers are not Engram measurements. See [contribution and publication rules](CONTRIBUTING.md).

## Development

Contributions use entirely invented fixtures and run the complete synthetic suite and lint before submission; publication checks the file allowlist, version, and evolution notes. See [contribution rules](CONTRIBUTING.md).

```sh
uv run pytest
uv run ruff check .
```

## Acknowledgements

The project uses SQLite, sqlite-vec, Apple MLX, and the MCP Python SDK. Cover illustrations were generated by an image model and processed for this project.

## References

1. Cormack, G. V., Clarke, C. L. A., & Buettcher, S. (2009). Reciprocal rank fusion outperforms condorcet and individual rank learning methods. SIGIR, 758–759. https://doi.org/10.1145/1571941.1572114

2. Garcia, A. sqlite-vec: A vector search SQLite extension. GitHub repository, dependency version 0.1.9. https://github.com/asg017/sqlite-vec
3. Zhang, Y., Li, M., Long, D., Zhang, X., Lin, H., Yang, B., Xie, P., Yang, A., Liu, D., Lin, J., Huang, F., & Zhou, J. (2025). Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation Models. arXiv. https://arxiv.org/abs/2506.05176

## Related research

The following sources are related by mechanism, not implementation dependencies or reproduced results; sleep/dream consolidation is only planned.

1. Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S. G., Stoica, I., & Gonzalez, J. E.（2023）. MemGPT: Towards LLMs as Operating Systems. arXiv. https://arxiv.org/abs/2310.08560 — Relates to layered memory and context management.
2. Martin, L.（2026）. The components of agent memory. Anthropic Research（工程团队）. https://www.anthropic.com/research/components-of-agent-memory — Relates to layered memory and corrections with provenance; its dreaming mechanism is not implemented here.
3. Anthropic（2025）. Effective context engineering for AI agents. Anthropic Engineering. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents — Relates to structured memory writes and on-demand retrieval.
4. Anthropic（2026）. New in Claude Managed Agents: dreaming, outcomes, and multiagent orchestration. Claude Blog. https://claude.com/blog/new-in-claude-managed-agents — Relates to planned sleep/dream consolidation, which is not implemented in this project.
5. Lin, K., Snell, C., Wang, Y., Packer, C., Wooders, S., Stoica, I., & Gonzalez, J. E.（2025）. Sleep-time Compute: Beyond Inference Scaling at Test-time. arXiv. https://arxiv.org/abs/2504.13171 — Relates to planned idle-time offline consolidation.
6. Behrouz, A., Zhong, P., & Mirrokni, V.（2024）. Titans: Learning to Memorize at Test Time. arXiv. https://arxiv.org/abs/2501.00663 — Relates to retention and short/long-term memory questions; this project does not implement its neural memory architecture.
7. Zhong, W., Guo, L., Gao, Q., Ye, H., & Wang, Y.（2024）. MemoryBank: Enhancing Large Language Models with Long-Term Memory. AAAI 2024. https://doi.org/10.1609/aaai.v38i17.29946 — Relates to memory archiving; this project neither invalidates facts automatically by age nor implements time decay.
8. Liu, S., Yao, Y., Jia, J., Casper, S., Baracaldo, N., Hase, P., Yao, Y., Liu, C. Y., Xu, X., Li, H., Varshney, K. R., Bansal, M., Koyejo, S., & Liu, Y.（2025）. Rethinking machine unlearning for large language models. Nature Machine Intelligence, 7, 181–194. https://doi.org/10.1038/s42256-025-00985-0 — Relates to forgetting criteria and validation limits; archiving records does not remove knowledge from model weights.
9. Park, J. S., O'Brien, J., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S.（2023）. Generative Agents: Interactive Simulacra of Human Behavior. UIST '23. https://doi.org/10.1145/3586183.3606763 — Relates to periodic reflection and bounded autonomous maintenance.
10. Xu, W., Liang, Z., Mei, K., Gao, H., Tan, J., & Zhang, Y.（2025）. A-MEM: Agentic Memory for LLM Agents. NeurIPS 2025. https://arxiv.org/abs/2502.12110 — Relates to automatic nearest-neighbor links after vector writes.
11. Chhikara, P., Khant, D., Aryan, S., Singh, T., & Yadav, D.（2025）. Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory. arXiv. https://arxiv.org/abs/2504.19413 — Relates to memory revision and maintenance within a bounded operation set.
12. Rasmussen, P., Paliychuk, P., Beauvais, T., Ryan, J., & Chalef, D.（2025）. Zep: A Temporal Knowledge Graph Architecture for Agent Memory. arXiv. https://arxiv.org/abs/2501.13956 — Relates to temporal validity and version provenance.
13. Zhang, Z., Dai, Q., Bo, X., Ma, C., Li, R., Chen, X., Zhu, J., Dong, Z., & Wen, J.-R.（2025）. A Survey on the Memory Mechanism of Large Language Model-based Agents. ACM Transactions on Information Systems, 43(6). https://doi.org/10.1145/3748302 — Provides a research framework for agent memory mechanisms.
14. Malkov, Y. A., & Yashunin, D. A.（2020）. Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs. IEEE TPAMI, 42(4), 824–836. https://doi.org/10.1109/TPAMI.2018.2889473 — Provides a nearest-neighbor algorithm comparison; this project uses brute-force search through sqlite-vec vec0, not HNSW.
15. Wilson, M. A., & McNaughton, B. L.（1994）. Reactivation of hippocampal ensemble memories during sleep. Science, 265(5172), 676–679. https://doi.org/10.1126/science.8036517 — Relates only to planned offline consolidation, not evidence of an implemented mechanism or biological equivalence.
16. McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C.（1995）. Why there are complementary learning systems in the hippocampus and neocortex. Psychological Review, 102(3), 419–457. https://doi.org/10.1037/0033-295X.102.3.419 — Relates only to planned offline consolidation and layered-memory ideas, not an implementation of biological learning systems.

## Repository structure

```text
src/engram/    Core source
tests/        Synthetic tests and publication checks
examples/     Invented-data examples
docs/         Architecture, evolution, research directions
docs/evolution/ Version evolution notes
scripts/      Publication boundary checks
readme-assets/ Cover assets
```

---

Maintained by <picture><source media="(prefers-color-scheme: dark)" srcset="readme-assets/em-logo-dark.svg"><img src="readme-assets/em-logo-light.svg" alt="EM" height="13"></picture> <b>EricMingle69</b> · <a href="https://github.com/Ming-Sir-69">Ming-Sir-69</a><br>

[MIT License](LICENSE) · Copyright (c) 2026 Eric Mingle.

Personal attribution does not grant owner identity or extend the license; see [personal notice](PERSONAL-NOTICE.md).
