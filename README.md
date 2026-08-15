# engram

本地优先的个人知识库，为 AI Agent 而设计。

名字取自神经科学术语 engram —— 记忆在大脑中留下的物理痕迹。库里每条记录就是一个 engram。

## 它解决什么问题

当多个 AI Agent（Claude Code、Codex、各类 Agent 平台）共同维护一个知识库时，最大的问题不是存储，而是**每个 Agent 对"这条内容该放哪"有自己的理解**。分类随平台和版本漂移，久而久之知识库就乱了。

engram 的做法是取消这个决策点：所有内容都是一条 record，没有"放哪里"可选。分类由系统统一裁决，写入方只负责提供内容。

## 特性

- **写入无需判断分类** —— 调用方只提供内容和可选的项目上下文
- **写入永不失败** —— 正文与全文索引同步落库；分类与向量由后续调用顺带补全。本地模型没启动也照样能写
- **四层分类降级** —— 结构信号 → k 近邻继承 → 本地模型 → 规则默认并标记待复核
- **三种检索模式** —— 关键词、向量、RRF 混合
- **中文友好** —— 自带中文分词，标识符与路径同时保留完整形式和拆分形式
- **完全本地** —— 数据、嵌入、分类全在本机，不依赖云端服务
- **数据在仓库之外** —— 默认位于 `~/second-brain-data/`，不靠 `.gitignore` 兜底

## 安装

```bash
uv sync --all-groups
```

需要 Python 3.13。语义功能需要本机运行 [Ollama](https://ollama.com)，并拉取嵌入模型：

```bash
ollama pull nomic-embed-text-v2-moe
```

## 快速开始

```bash
# 写入一条记录
engram record create --title "标题" --body "正文内容" --project my-project

# 补全语义层（分类、向量、链接）
engram index drain

# 检索
engram search "查询词" --mode hybrid --top-k 5

# 查看状态与积压
engram status
```

所有命令默认输出紧凑 JSON，便于 Agent 解析；加 `--human` 输出便于阅读的格式。

## 配置

| 环境变量 | 默认值 |
|---|---|
| `ENGRAM_DATA_DIR` | `~/second-brain-data` |
| `ENGRAM_SOURCE_DIR` | `~/.claude/rules/second-brain` |
| `ENGRAM_SEED_TAXONOMY` | `$ENGRAM_DATA_DIR/config/seed_taxonomy.json` |
| `ENGRAM_EMBEDDING_MODEL` | `nomic-embed-text-v2-moe` |
| `ENGRAM_EMBEDDING_DIMENSIONS` | `768` |
| `ENGRAM_CLASSIFIER_MODEL` | `qwen3.5:4b` |
| `ENGRAM_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |

## 从 Markdown 迁移

把既有笔记一次性迁入数据库。迁移只读源文件，不写、不改、不重命名；靠内容哈希
保证幂等，可以先看清楚再真跑：

```bash
engram migrate from-markdown --dry-run
engram migrate from-markdown
```

笔记里的 `##` / `###` 标题通常已经是长期人工维护的分类结果。给出一张标题映射表，
迁移时会把它转成高置信度标签写入，省掉一轮模型推断：

```json
{
  "机械结构": { "domains": ["product-design"], "tags": ["mechanism-design"] }
}
```

放到 `$ENGRAM_DATA_DIR/config/seed_taxonomy.json`（或用 `--taxonomy` 指定）。
标题因人而异，因此这张表属于你的配置，不随程序发布。没有它迁移照常进行，
报告里的 `unseeded` 会等于总条数——这个数字就是映射表的覆盖率。

## 检索质量回归

换嵌入模型、改分词或调融合权重之后，用一组固定问题证明检索没有变差。门槛由
退出码判定，可直接挂进脚本：

```bash
engram bench recall --gold gold.json --mode hybrid --top-k 5 --min-hits 28
```

## 设计原则

- **写入端最小化，输出端计算化** —— "这条记录接下来要做什么"由消费方读取时决定，不作为写入时的负担
- **取消决策点优于增加校验** —— 让错误无法表达，而不是拦截错误
- **外部 Agent 可触发，不可裁决** —— 分类判据必须长期一致，不能随调用方漂移

## 开发

```bash
uv run pytest
uv run ruff check .
```

## License

MIT
