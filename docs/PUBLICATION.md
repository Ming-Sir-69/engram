# 公开发布与持续维护 / Public publication and maintenance

本仓公开自有实现、架构、设计决策、研究方向和按版本整理的演化说明。公开实现的版本由 `PUBLICATION.toml` 指定，和私有运维部署指纹、内部包版本、MCP/接口版本分别说明。历史设计阶段没有实际公开 tag/release 的，不补造过去版本。

This repository publishes reusable implementation, architecture, design decisions, research hypotheses, and versioned evolution notes. A public release version is distinct from a private deployment fingerprint and separately documented interface/package lineage. Undelivered historical stages are not retroactively labeled as releases.

## 不可上传的数据 / Data that must never be published

实际记忆、任务、会话、上下文、反馈、HCD、事件记录及原始运行日志，经过匿名或脱敏仍不可上传。数据库、日志/JSONL、私有配置/凭据、内部地址、生产回执、训练数据、模型权重及运维目录不能成为公开 payload。所有示例和测试输入必须从零合成；禁止从真实记录改写、截取、匿名化或搬运。不要把私人目录、工作树、真实数据或日常运维材料交给公开导出脚本。

Actual records and runtime logs remain prohibited after anonymization or redaction. Examples and test inputs must be created synthetically from scratch. Runtime databases, private configuration, host addresses, receipts, training data, model weights, and operations materials are outside the public boundary.

## 精确文件白名单 / Exact file allowlist

`PUBLIC-FILES.json` 的格式仅允许 `{"schema": 1, "files": ["PUBLIC-FILES.json", "PUBLICATION.toml", "..."]}`。每个路径是明确的仓库相对文件，不能用目录、glob、软链接、绝对路径或忽略规则。它必须列出自身、发布配置、版本源、CHANGELOG 和当版演化说明。新增公开文件须人工核对来源和内容，再显式加入白名单；不要自动把整树文件加入。

Only exact relative files listed in `PUBLIC-FILES.json` may enter a payload. An unlisted file fails the check, even when its extension looks harmless. Root Git checkout metadata is excluded; nested Git metadata and symlinks are rejected. Arbitrary data JSON is prohibited; the manifest, structurally checked `*.schema.json`, and precisely registered synthetic config files are the only JSON exceptions.

合成配置 JSON 须在 `PUBLICATION.toml` 的 `[publication.synthetic_config_files]` 表逐路径指定对应 `*.schema.json`，两者都进入文件白名单。配置 schema 必须逐层为封闭对象、列明字段类型，字符串只能是固定常量/枚举，数字必须限定范围；配置例外不支持数组或记录正文/标识字段。实际数据不能靠标注 synthetic 变成例外。配置文本和 schema 常量仍执行全部内容扫描。通用 localhost/127.0.0.1/::1 服务地址属于源码实现可用，内部宿主和其他私人 IP 仍拒绝。

## 每次维护 / Every maintenance change

1. 只在专门准备的公开源码树中改动。私人运行树不作为公开来源。
2. 提高公开 SemVer。任何白名单内容变化，包括公开文档和发布检查脚本，都应更新版本。
3. 在 `CHANGELOG.md` 增加 `## [VERSION] - YYYY-MM-DD` 当前版本段。
4. 增加 `docs/evolution/VERSION.md`，说明问题、架构与接口、决策与替代方案、研究假设、验证与边界、下一方向。旧版演化记录保留。
5. 检查来源/许可/公开文件清单，执行公开实现所需的合成测试和发布边界测试。
6. 使用上一个真实公开 tag 或已审核的主分支提交作为 Git 基线进行版本检查；不得伪造旧 tag。第一版没有历史基线时，仍须通过全部文件、内容和当前版本文档检查。
7. 两个独立 Agent 审核实现范围、许可、隐私类别和版本说明；分歧先解决再发布。审核回执保留在仓库外的私有工作目录，不能复制实际记录或把回执装入 release。
8. 从通过检查的白名单生成 payload，并由当版 CHANGELOG 段生成 GitHub Release 说明。发布只提交此安全源码树，tag/Release 对应这个公开版本。

Every change must advance the configured public version and update its release/evolution notes. Review implementation scope, licensing, privacy categories, and release content with two independent agents. Run synthetic tests and the boundary check against the last real public release or reviewed main commit. Private review receipts and operational fingerprints remain outside GitHub.

## 本地检查与发布 / Local checks and release preparation

需要 Python 3.11+，发布检查只依赖标准库。下列路径均使用专门的公开源码树；外部输出目录须不存在。实际项目测试另按项目依赖执行。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_publication_guard.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/publication_check.py check --root . --base-ref PUBLIC_BASE
PYTHONDONTWRITEBYTECODE=1 python3 scripts/publication_check.py build --root . --base-ref PUBLIC_BASE --output ../release-payload --manifest-out ../public-payload-manifest.json
PYTHONDONTWRITEBYTECODE=1 python3 scripts/publication_check.py release-notes --root . --base-ref PUBLIC_BASE --output ../public-release-notes.md
```

`PUBLIC_BASE` 必须替换成已核实的真实 Git ref/提交。新仓的首次公开树没有历史提交时省略 `--base-ref`；后续更新不能省略。输出 manifest 只包含公开文件路径、字节数和 SHA-256，不含运行数据；它保存在源码树和 payload 外。导出复制的是已经扫描过的字节，不递归打包工作目录。工具不会推送、创建 tag/Release、读取日常运行目录，也不会设置定时器或后台同步。

The emitted manifest contains only public file names, sizes, and SHA-256 values. Outputs must be outside the source tree; the manifest is also outside the payload. This script prepares reviewed bytes and the exact release section. It does not publish, schedule jobs, collect runtime data, or authorize new actions.

## PR/CI 门槛与检查边界 / CI gate and scanner limits

`.github/workflows/publication-boundary.yml` 使用 `pull_request` 与主分支 push，只有只读 GitHub 权限。CI 只读本仓公开树，运行合成发布测试和以基线为参照的检查，不连私人数据库/宿主、不下载模型、不使用生产凭据，也不使用 `pull_request_target`。项目实现测试可使用独立 workflow；平台依赖必须说明。

检查拒绝已知凭据格式、个人绝对路径、非保留合成 UUID/明显记录 ID、内部 IP/域名、未知 URL、禁止格式/目录、二进制、白名单遗漏和缺失版本说明。普通 schema 字段与源码 regex 可以公开；明确保留的合成 UUID、标准文档示例域/IP 可用。不能通过 ignore 或一行 synthetic 标签解除实际字段检测。新增公开 URL 域只用于核实过的公开资料，内部域/IP和凭据仍被硬规则阻挡。

Static checks are a release gate, not proof of provenance or absence of every possible secret encoding. A text blob without recognizable markers cannot be identified as an actual record by regex alone. Independent source/provenance review remains mandatory, and all examples must have synthetic provenance. A passing scan does not establish causal benefit or production validation of research hypotheses.

维护流程不代表定时自动同步或整库上传授权。用户没有请求时不创建后台采集、同步或自动发布任务。
