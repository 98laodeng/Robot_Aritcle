# 机器人操作论文雷达

本机看板，用来跟踪机器人操作相关论文。每篇入库论文都会带上可解释的入选理由，例如会议 Oral、实验室、主题或 Awesome 列表。这里不打分，也不做排序推荐。

时间窗默认是 `2024-10-08` 到 `2026-10-08`，写在 `config.yaml` 的 `window` 里。数据存在本地 SQLite（`data/papers.db`）。

## 什么会入库

一篇论文至少要命中下面一条才会留下：

- 会议明确标成 oral、spotlight、highlight，或获奖。奖项单独记录，不拿来升降展示类型。
- RSS 录用论文。RSS 的展示类型默认是 spotlight，理由是「RSS 接收」，不会被当成会议 Oral。
- 目标实验室在论文发表时的单位（OpenAlex 的 authorships，不是作者现在的单位）。
- arXiv / OpenAlex 上命中主题规则。强标题词直接保留；弱标题词只在 `cs.RO` 且摘要也命中时保留。
- Awesome 列表里、位于纳入章节的条目。VLN、模拟器、数据集等排除章节不会因为出现在列表里就入库。

期刊（Science Robotics、IJRR、TRO）只在同时命中实验室时入库。ICRA、IROS 官网在 2026-10-08 返回 403，短口头报告也不会被当成 Oral；这两处主要靠 arXiv 主题和 OpenAlex 实验室补进来。

首页「重点」是：会议 oral / spotlight / highlight / award，或者同时有实验室和主题。单独的 RSS 或单独的主题不算重点。

## 环境

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

可选：设置 `SEMANTIC_SCHOLAR_API_KEY`，用来补缺失摘要。没有密钥时遇到 429 会停掉这一轮补摘要。

## 看板

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

打开 http://127.0.0.1:8765 。进程开着时，每 6 小时做一次增量同步，每天 03:15 做一次对账。

- 首页是未读的重点。
- 「收藏」和已读互相独立。卡片和详情页都能切换，收藏页按收藏时间倒序。
- 「同步」页可以手动跑增量、对账或回填，并查看游标和最近一次运行。

## 命令行同步

```bash
# 增量：arXiv 游标、Awesome 内容哈希、OpenAlex 最新一页、期刊、OpenReview
uv run python -m app sync

# 对账：重读会议 JSON 和 RSS，arXiv 回看最近 180 天且不推进正向游标
uv run python -m app sync --reconcile

# 回填整个时间窗。可以反复运行，游标会接着上次继续
uv run python -m app sync --backfill

# 只跑一部分源。名字：virtual,rss,awesome,arxiv,openalex,journal,openreview,program,s2
uv run python -m app sync --reconcile --only virtual,rss --venue CVPR --year 2026
```

增量不会重读会议 JSON、RSS 和程序页，这些放在对账和回填里。OpenReview API 若连续失败 5 次，会暂停 24 小时，暂停期间不再请求。

## 改规则

实验室、主题词、会议 JSON 地址、Awesome 章节都在 `config.yaml`。加一个实验室或一条强/弱标题词，改这个文件即可，不用改代码。

`mailto` 会放进 Crossref 和 OpenAlex 的请求里，建议改成真实邮箱。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest
```

需要关掉插件自动加载，否则本机 ROS 的 `launch_testing` 会在收集测试时失败。
