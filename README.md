<picture>
  <source media="(prefers-color-scheme: dark)" srcset="readme-assets/header-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="readme-assets/header-light.svg">
  <img alt="engram · Agent 共用本地知识库 · ✦ EricMingle69" src="readme-assets/header-light.svg" width="100%">
</picture>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README.en.md">English</a> · <a href="PERSONAL-NOTICE.md">✦ EricMingle69</a>
</p>

# engram · Agent 共用本地知识库

本地优先的个人知识库：调用方提供内容，系统统一处理分类。
正文与全文索引同步落库，分类和向量后续补全；数据默认位于仓库外的 `~/second-brain-data/`。

## 安装与第一个记录

需要 Python 3.13+ 与 uv，在仓库根目录执行：

```sh
uv sync --all-groups
uv run engram record create --title "笔记" --body "正文内容" --project my-project
uv run engram search "笔记" --mode keyword --top-k 5
uv run engram --human status
```

默认输出紧凑 JSON；`--human` 是顶层参数，应放在子命令前。
关键词检索不依赖语义模型；文件系统、数据库和配置错误仍需处理。

## 可选的语义层

准备本机 Ollama 与嵌入模型，再补全分类、向量和链接：

```sh
ollama pull nomic-embed-text-v2-moe
uv run engram index drain
uv run engram search "查询词" --mode hybrid --top-k 5
```

向量与混合检索依赖本机模型。`ENGRAM_DATA_DIR` 可改变数据位置；模型、维度与分类配置应保持一致。

## 接入 Agent

```sh
uv run engram mcp
```

stdio MCP 提供 `remember`、`recall`、`get`、`status`。
宿主不会自动激活项目虚拟环境；MCP 的 `command` 应填本机实际的绝对路径，例如仓库内 `.venv/bin/engram`，参数为 `["mcp"]`。

## 迁移与导出

先以 `uv run engram migrate from-markdown --dry-run`检查迁移，再决定正式导入。
`ENGRAM_EXPORT_DIR` 启用派生 Markdown；`ENGRAM_INDEX_PATH` 启用轻量索引。
导出拒绝覆盖非本工具生成文件，显式 `--adopt` 才接管目录并先保存原件。

## 实现与许可

[pyproject.toml](pyproject.toml)定义依赖与 CLI，[src/engram](src/engram/)是实现，[tests](tests/)是验证入口。
知识库正文、导出和本机配置不应作为公开问题附件。
[MIT License](LICENSE)：Copyright (c) 2026 Eric Mingle。

---

文档维护：**✦ EricMingle69** · [Ming-Sir-69](https://github.com/Ming-Sir-69)  
[个人标识、许可与权限说明](PERSONAL-NOTICE.md) · 明暗页眉随 GitHub 主题自动切换。
