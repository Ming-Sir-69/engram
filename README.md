<picture>
  <source media="(prefers-color-scheme: dark)" srcset="readme-assets/cover-zh-dark.png">
  <img alt="Engram" src="readme-assets/cover-zh-light.png" width="100%">
</picture>

<p align="center"><a href="docs/architecture.md">架构</a> · <a href="docs/research-directions.md">研究方向</a> · <a href="CHANGELOG.md">版本记录</a> · <a href="CONTRIBUTING.md">贡献</a></p>
<p align="center">
<img alt="MIT" src="https://img.shields.io/badge/license-MIT-F6C555?style=flat-square&amp;labelColor=2E2A26">
<img alt="Version" src="https://img.shields.io/badge/version-0.4.5-6B655C?style=flat-square&amp;labelColor=2E2A26">
<img alt="Python" src="https://img.shields.io/badge/Python-3.13%2B-6B655C?style=flat-square&amp;labelColor=2E2A26">
<img alt="SQLite" src="https://img.shields.io/badge/storage-SQLite-A8A196?style=flat-square&amp;labelColor=2E2A26">
</p>
<p align="center"><b>简体中文</b> · <a href="README.en.md">English</a></p>

# Engram · Agent 共用本地知识库

Engram 0.4.5 把长期知识保存在本机 SQLite 中，供不同 Agent 使用同一份事实源。写入方提供内容，分类与向量在后续补全；明确纠正形成可追溯关系，读取时区分当前适用内容与历史原文。

此前公开代码标为 0.1.0，没有正式版本标签。0.4.0 将当前核心整理为公开源码，0.4.1 更新 CI、文档及其发布检查，0.4.2 更新署名与封面；历史阶段按里程碑描述，不补造 0.2/0.3 发布。见 [版本变化](CHANGELOG.md)、[架构](docs/architecture.md)、[演化历史](docs/evolution.md) 和 [研究方向](docs/research-directions.md)。


## 主要功能

- 正文、项目绑定、修订、FTS 与 outbox 同事务提交。ID 与关键词提交后可读，向量和整理状态不作为可读门槛。
- `remember` 的可选 `corrections` 明确目标、整条/局部范围和来源。整条替代记为 `supersedes`，局部更正记为 `corrects`。局部精确替换要求基线一致、片段唯一且范围不重叠。
- `recall(view="current")` 默认返回当前适用投影；`history` 与 `get` 保留历史原文。旧关键词、向量或关联边可沿明确纠正链解析到当前记录。
- 项目是可选的有限排序偏置，无注册要求；缺项目元数据仍可按正文检索。年龄或语义相似度不自动证明旧事实失效。
- 关键词、向量和 RRF 混合检索；语义故障明确退回关键词并返回降级信息。MLX 模型按需加载，共享本机嵌入接口串行推理。
- stdio MCP 与可选官方 SDK HTTP/SSE 接入；远程认证保留 Bearer、OAuth、PKCE、resource、Host/Origin 与本人授权边界。
- Claude Code 命令 Hook 可在相关上下文出现时进行有边界的关键词检索，按修订与纠正链变化再次提示，错误时放行。此包不安装个人 Hook 配置，也不宣称所有宿主对话均自动采用记忆。

- 写入向量后按 kNN 近邻自动建立双向关联；关联分数为 `1/(1+distance)`，默认低于 0.5 不建边。
- 受限操作集内的自主维护支持修订、归档与恢复记录，包含乐观并发检查和回滚，默认关闭；归档不等于按时间衰减遗忘。

## 快速开始

需要 Python 3.13+、uv。当前依赖使用 Apple MLX，支持 Apple Silicon macOS；关键词和写入路径不加载模型。

向量层还要求 Python 的 SQLite 启用扩展加载（`enable_load_extension`）；部分 macOS Python 发行版未启用此功能。下面使用 uv 管理的 Python；CI 会先检查这项能力，再运行全套合成测试。

```sh
uv sync --managed-python --all-groups --extra remote
uv run python examples/synthetic_corrections.py
uv run pytest
uv run ruff check .
```

示例仅创建临时合成传感器数据，演示“旧关键词 → 新值”，结束后删除临时库。`--offline` 的确定性向量用于工程测试，不能代表语义模型效果。

独立记忆库设置 `ENGRAM_DATA_DIR`，保持数据目录在代码仓库外。以下内容也是合成示例：

```sh
export ENGRAM_DATA_DIR="$HOME/engram-data"
export ENGRAM_USAGE_TELEMETRY=0
uv run engram record create --title "Synthetic sensor" --body "synthetic-prism channel=amber"
uv run engram search "synthetic-prism" --mode keyword
uv run engram --human status
```

## 用法

```sh
uv run engram mcp
```

标准工具包含 `remember`、`recall`、`get`、`status` 和独立 `feedback`。Claude profile 共享同一合同；远程启动时须自行配置认证，不能将未认证接口直接放上公网。启动参数见 `uv run engram mcp --help`。

默认嵌入模型属于 Qwen3 Embedding 系列。<sup>[[3]](#参考文献)</sup>

真实语义模型须由使用者另行准备在 `$ENGRAM_DATA_DIR/models/`，默认模型为 `Qwen3-Embedding-0.6B` 与 `Qwen3-1.7B-4bit`。模型缺失不会自动下载。`index drain` 补全，`index rebuild` 先暂存整批向量，再检查源内容一致性并替换旧索引。

## 设计与原理

本项目以 SQLite 作为事实源，正文与全文索引随事务提交；语义层稍后补全。混合检索使用倒数排名融合（RRF），将不同检索列表按排名合并。<sup>[[1]](#参考文献)</sup>

向量表使用 sqlite-vec 的 `vec0`。<sup>[[2]](#参考文献)</sup>

[架构与公开副本差异](docs/architecture.md) · [演化历史](docs/evolution.md)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="readme-assets/write-path-zh-dark.svg">
  <img alt="Write and indexing sequence" src="readme-assets/write-path-zh-light.svg" width="100%">
</picture>

## 局限

本包含自有核心源码、合成测试、示例与文档，不含实际记忆、日志、反馈、上下文、生产回执、个人配置、运维运行器或权重。受控内容维护、观测与固定证据模块是可复用源码，示例默认关闭观测及自动维护。源码自改进 `source_improvement` 依赖部署方另行提供的网关，本包不提供该流水线，默认不暴露该工具。

公开副本把设备验证器名称改为通用标识；运行观测时间表改为显式 `ENGRAM_RUN_SCHEDULES`，默认只有手动入口。认证与访问边界保留并强化：ASGI 前缀不能绕过 Bearer 或显式队列/超时限制；仅超时配置可正常释放请求；初始化图标声明与 SVG 响应一致；同 client/resource 的 spent refresh 重放会撤销活动 family，关联按哈希键保存且到期时间不超过旧令牌原始期限，不保存旧令牌明文。修复只针对本公共副本，原私有部署未改。详见 [架构与公开副本差异](docs/architecture.md)。

合成工程测试验证合同、反例和隔离行为；它们不能证明真实用户收益、长期检索精度或所有宿主采用率。研究论文成绩也不作为本库的体验成绩。贡献与持续版本维护见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 开发

贡献须使用完全虚构的数据；提交前运行完整合成测试与 lint，发布按文件清单检查版本和演化记录。见 [贡献规则](CONTRIBUTING.md)。

```sh
uv run pytest
uv run ruff check .
```

## 致谢

本项目使用 SQLite、sqlite-vec、Apple MLX 与 MCP Python SDK。封面插画由图像生成模型生成，经本项目处理。

## 参考文献

1. Cormack, G. V., Clarke, C. L. A., & Buettcher, S. (2009). Reciprocal rank fusion outperforms condorcet and individual rank learning methods. SIGIR, 758–759. https://doi.org/10.1145/1571941.1572114

2. Garcia, A. sqlite-vec: A vector search SQLite extension. GitHub repository, dependency version 0.1.9. https://github.com/asg017/sqlite-vec
3. Zhang, Y., Li, M., Long, D., Zhang, X., Lin, H., Yang, B., Xie, P., Yang, A., Liu, D., Lin, J., Huang, F., & Zhou, J. (2025). Qwen3 Embedding: Advancing Text Embedding and Reranking Through Foundation Models. arXiv. https://arxiv.org/abs/2506.05176

## 相关研究

以下文献按机制对应列出，不表示本项目按其实现或复现其结果；睡眠/做梦整理仅为规划。

1. Packer, C., Wooders, S., Lin, K., Fang, V., Patil, S. G., Stoica, I., & Gonzalez, J. E.（2023）. MemGPT: Towards LLMs as Operating Systems. arXiv. https://arxiv.org/abs/2310.08560 — 对应分层记忆与上下文管理。
2. Martin, L.（2026）. The components of agent memory. Anthropic Research（工程团队）. https://www.anthropic.com/research/components-of-agent-memory — 对应分层记忆和带来源纠正，其中 dreaming 机制本项目尚未实现。
3. Anthropic（2025）. Effective context engineering for AI agents. Anthropic Engineering. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents — 对应结构化记忆写入与按需检索。
4. Anthropic（2026）. New in Claude Managed Agents: dreaming, outcomes, and multiagent orchestration. Claude Blog. https://claude.com/blog/new-in-claude-managed-agents — 对应规划中的睡眠/做梦整理，本项目尚未实现该功能。
5. Lin, K., Snell, C., Wang, Y., Packer, C., Wooders, S., Stoica, I., & Gonzalez, J. E.（2025）. Sleep-time Compute: Beyond Inference Scaling at Test-time. arXiv. https://arxiv.org/abs/2504.13171 — 对应规划中的空闲时离线整理。
6. Behrouz, A., Zhong, P., & Mirrokni, V.（2024）. Titans: Learning to Memorize at Test Time. arXiv. https://arxiv.org/abs/2501.00663 — 对应记忆保留与长短期分层的研究问题，本项目未实现其神经记忆架构。
7. Zhong, W., Guo, L., Gao, Q., Ye, H., & Wang, Y.（2024）. MemoryBank: Enhancing Large Language Models with Long-Term Memory. AAAI 2024. https://doi.org/10.1609/aaai.v38i17.29946 — 对应记忆归档的研究问题，本项目不按年龄自动判定事实失效，也不实现时间衰减。
8. Liu, S., Yao, Y., Jia, J., Casper, S., Baracaldo, N., Hase, P., Yao, Y., Liu, C. Y., Xu, X., Li, H., Varshney, K. R., Bansal, M., Koyejo, S., & Liu, Y.（2025）. Rethinking machine unlearning for large language models. Nature Machine Intelligence, 7, 181–194. https://doi.org/10.1038/s42256-025-00985-0 — 对应遗忘判定与验证边界，本项目归档记录不等于从模型权重中删除知识。
9. Park, J. S., O'Brien, J., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S.（2023）. Generative Agents: Interactive Simulacra of Human Behavior. UIST '23. https://doi.org/10.1145/3586183.3606763 — 对应周期性反思与受限自主维护的研究问题。
10. Xu, W., Liang, Z., Mei, K., Gao, H., Tan, J., & Zhang, Y.（2025）. A-MEM: Agentic Memory for LLM Agents. NeurIPS 2025. https://arxiv.org/abs/2502.12110 — 对应写入向量后自动建立近邻关联。
11. Chhikara, P., Khant, D., Aryan, S., Singh, T., & Yadav, D.（2025）. Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory. arXiv. https://arxiv.org/abs/2504.19413 — 对应受限操作集内的记忆修订与维护。
12. Rasmussen, P., Paliychuk, P., Beauvais, T., Ryan, J., & Chalef, D.（2025）. Zep: A Temporal Knowledge Graph Architecture for Agent Memory. arXiv. https://arxiv.org/abs/2501.13956 — 对应事实时效与版本溯源。
13. Zhang, Z., Dai, Q., Bo, X., Ma, C., Li, R., Chen, X., Zhu, J., Dong, Z., & Wen, J.-R.（2025）. A Survey on the Memory Mechanism of Large Language Model-based Agents. ACM Transactions on Information Systems, 43(6). https://doi.org/10.1145/3748302 — 对应 Agent 记忆机制的总体研究框架。
14. Malkov, Y. A., & Yashunin, D. A.（2020）. Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs. IEEE TPAMI, 42(4), 824–836. https://doi.org/10.1109/TPAMI.2018.2889473 — 对应近邻检索的算法比较，本项目使用 sqlite-vec 的 vec0 暴力近邻检索，不使用 HNSW。
15. Wilson, M. A., & McNaughton, B. L.（1994）. Reactivation of hippocampal ensemble memories during sleep. Science, 265(5172), 676–679. https://doi.org/10.1126/science.8036517 — 仅对应规划中的离线整理意象，不作为已实现机制或生物学等效性的证据。
16. McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C.（1995）. Why there are complementary learning systems in the hippocampus and neocortex. Psychological Review, 102(3), 419–457. https://doi.org/10.1037/0033-295X.102.3.419 — 仅对应规划中的离线整理与分层记忆意象，不表示本项目实现了生物神经学习系统。

## 仓库结构

```text
src/engram/    核心源码
tests/        合成测试与发布检查
examples/     虚构数据示例
docs/         架构、演化与研究方向
docs/evolution/ 版本演化记录
scripts/      发布边界检查
readme-assets/ 封面素材
```

---

文档维护：<picture><source media="(prefers-color-scheme: dark)" srcset="readme-assets/em-logo-dark.svg"><img src="readme-assets/em-logo-light.svg" alt="EM" height="13"></picture> <b>EricMingle69</b> · <a href="https://github.com/Ming-Sir-69">Ming-Sir-69</a><br>

[MIT License](LICENSE) · Copyright (c) 2026 Eric Mingle.

个人标识不扩大许可或授予本人身份，见 [个人标识说明](PERSONAL-NOTICE.md)。
