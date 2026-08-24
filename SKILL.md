---
name: xiaohongshu-harvester
description: 小红书公开/半公开内容采集与本地化分析 skill。能力：关键词搜索笔记、单篇笔记详情 + 评论、用户主页笔记、热门榜、用户搜索，以及 collect→clean→enrich→analyze 全链路，输出洞察式 Markdown 报告 + CSV 明细。适用：用户要求抓取/分析/导出小红书笔记、评论、用户、热门榜数据时使用。前提：用户提供本人账号的登录 cookie。约束：不绕过登录态、不并发不高频、拒绝绕过签名风控或未授权数据的抓取。
---

# 小红书采集分析 skill — Agent 操作手册

> 本文档是给 agent 的操作指南: 什么场景用什么命令、遇到什么问题怎么处理。
> 参考文档: `references/api.md`(接口实测) / `cookie.md`(cookie 维护) / `signing.md`(签名机制) / `output_schema.md`(字段定义)。

## 1. 能力与触发条件

| 用户意图 | 典型说法 | 状态 |
| --- | --- | --- |
| 关键词话题分析 | "抓取/分析 关键词 X"、"小红书都在聊什么" | ✅ 推荐 `--enrich-notes` |
| 单篇笔记详情 / 评论 | 给了笔记链接或 note_id | ✅ 需要 `xsec_token` |
| 用户主页笔记 | "某个用户的笔记"、"这个博主的帖子" | ✅ SSR 首屏 + 滚动加载 |
| 热门榜 / 热搜 | "热门榜"、"热搜词" | ✅ 页面驱动抓取; 频率敏感 |
| 搜索用户 | "搜一下博主 X" | ✅ 接口直接抓取, 单页 ~20 条 |

## 2. 开始前检查（每次任务必做）

1. **Python 环境**: 使用 `data-collect` conda 环境（含 requests/jieba/playwright），首次运行先 `python -m playwright install chromium`。所有命令在仓库根目录执行。
2. **Cookie**: `assets/cookies.json` 必须存在且有效。先跑 `python scripts/xhs_client.py whoami` 确认能解析。失效信号 `-101` / HTTP 401/403 → 停止并请用户重新导出（Chrome DevTools → Application → Cookies → 导出）。cookie 是敏感文件（.gitignore 已排除），**绝不写入 git / 聊天记录**。
3. **从用户消息解析参数**:
   - 笔记链接 `https://www.xiaohongshu.com/explore/<note_id>?xsec_token=<token>&...` → 提取 `note_id` 和 `xsec_token`（URL 里 `xsec_token=` 后面的值）。
   - 用户主页链接 `https://www.xiaohongshu.com/user/profile/<user_id>` → 提取 `user_id`。

## 3. 标准操作（按用户意图选命令）

### 3.1 关键词分析（最常用）
```bash
# 完整分析（推荐）: 搜索 + Top10 高互动笔记补全正文/标签/时间 + 评论
python scripts/pipeline.py --keyword "关键词" --pages 3 --enrich-notes 10 --with-comments --workspace data/runs

# 只要搜索 + 统计，不补全（快，但报告缺正文维度）
python scripts/pipeline.py --keyword "关键词" --pages 3 --workspace data/runs
```
输出到 `data/runs/<日期>_<topic>/`（7 个文件，见 §5）。跑完向用户汇报：**report.md 路径 + 核心结论要点**（不要只丢一个路径）。

### 3.2 单篇笔记 + 评论
```bash
python scripts/pipeline.py --note <note_id> --with-comments --xsec-token <token> --workspace data/runs
```
`xsec_token` 必须从笔记 URL 复制；缺失可能拿不到正文/评论。

### 3.3 用户主页笔记
```bash
python scripts/pipeline.py --user <user_id> --pages 2 --workspace data/runs
```

### 3.4 只要原始数据（不要报告）
```bash
python scripts/collect.py --keyword "关键词" --pages 3 --out data/raw/x.jsonl
```

### 3.5 分阶段调试
```bash
python scripts/collect.py --keyword "关键词" --pages 3 --out data/raw/x.jsonl
python scripts/clean.py   --in data/raw/x.jsonl --out data/clean/x.jsonl
python scripts/enrich.py  --in data/clean/x.jsonl --out data/enriched/x.jsonl
python scripts/analyze.py --in data/enriched/x.jsonl --report x.report.md --summary x.summary.json
```

## 4. 参数速查

| 参数 | 适用模式 | 说明 |
| --- | --- | --- |
| `--keyword` / `--pages` | keyword | 关键词与页数（默认 3 页 ≈ 60 条；搜索卡片无正文/时间戳） |
| `--enrich-notes N` | keyword | 对热度 Top N 补全正文/标签/时间戳（推荐 10） |
| `--with-comments` | keyword/note | 同时抓评论（补全时评论来自同一页面导航，不额外耗请求） |
| `--note` / `--user` / `--hotlist` / `--search-user` | 模式 | 互斥，必须且只能选一个 |
| `--xsec-token` | note | 笔记访问令牌 |
| `--topic` | 全部 | run folder 名字后缀（默认由 keyword/user/note 推断） |
| `--workspace` | 全部 (仅 pipeline.py) | 输出根目录（默认 `data/runs`，每次新建 `<日期>_<topic>` 子目录） |
| `--sign-engine` | 全部 | 默认 `browser`（页面驱动）；`node`/`legacy` 仅供调试，当前 XHS 拒绝 |
| `--category` | hotlist | 热门榜分类，默认 `general` |
| `--page-size` | keyword / hotlist | 单页笔记/热门词条数，默认 20 / 50 |
| `--sort` | keyword | 搜索排序：`general`（默认）/`popular`/`time_descending` |
| `--max-comment-pages` | note (with --with-comments) | 评论翻页数，默认 3 |

## 5. 输出物（run folder）

| 文件 | 内容 |
| --- | --- |
| `raw.jsonl` / `clean.jsonl` / `enriched.jsonl` | collect / clean / enrich 各阶段数据 |
| `report.md` | 洞察式报告：核心结论 / 数据质量 / 互动热度分布(直方图) / 关键词 / 话题×互动 / 情感×互动 / Top 笔记(正文摘要) / Top 用户 / 评论分析 / 风险 / 方法论 |
| `summary.json` | 机器可读统计 |
| `notes.csv` / `comments.csv` | Excel 可直接打开的明细（UTF-8 BOM） |

## 6. 故障排查（Agent 决策表）

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 搜索返回 `code:300011` | 旧 v1 接口废弃（内置已是 v2 页面驱动） | 无需处理；若仍出现，确认没被手动切成 node/legacy 引擎 |
| `HTTP 406` / `Failed to fetch` | raw fetch 被网关拒 / 页面 fetch 包装偶发 | 走页面驱动路径；降低频率，等几分钟重试 |
| `-101` / HTTP 401/403 | cookie 失效 | **停止**，请用户重新导出 cookie |
| `-102` / 连续空数据 | 风控触发 | 等 60s 重试 1 次，仍失败则退出并提示 |
| 笔记详情拿不到正文 | `xsec_token` 缺失/失效 | 从笔记 URL 复制 xsec_token 重试 |
| 报告缺正文/话题/时间分布 | v2 搜索卡片不含正文（接口限制） | 加 `--enrich-notes N` 补全 Top 笔记 |
| 用户主页只有少量笔记 | SSR 首屏 + 滚动加载，或该用户笔记少 | `--pages` 触发滚动；数据量本身受页面限制 |
| `--hotlist` 报错 / 空数据 | 热门榜页面驱动触发风控或路径变更 | 等几分钟降频重试；持续失败再排查页面路径 |

## 7. 红线与合规（必须遵守）

- 仅使用用户本人账号的 cookie；**不绕过登录态、不绕过签名风控**。
- 默认限速 1–2s/请求，抓取 ≤3 页，不并发。
- 一旦出现 `-101` / 登录失效 / 账号冻结信号，立即停止并提示，不自动重试打爆接口。
- cookie 不入 git / 聊天记录 / 共享位置；数据仅供用户本地分析。

## 8. 命令速查（完整）

```bash
conda activate data-collect
python -m playwright install chromium   # 首次

# cookie 检查 / 加载
python scripts/xhs_client.py whoami
python scripts/xhs_client.py init-cookie

# 关键词 + 补全 + 评论（推荐完整流程）
python scripts/pipeline.py --keyword "hc 缩减" --pages 3 --enrich-notes 10 --with-comments --workspace data/runs

# 单篇笔记 + 评论
python scripts/pipeline.py --note <note_id> --with-comments --xsec-token <token> --workspace data/runs

# 用户主页
python scripts/pipeline.py --user <user_id> --pages 2 --workspace data/runs

# 热门榜 (页面驱动, 频率敏感)
python scripts/pipeline.py --hotlist --category general --workspace data/runs

# 关键词搜用户
python scripts/pipeline.py --search-user "小红书博主" --pages 1 --workspace data/runs

# 只采集不分析
python scripts/collect.py --keyword "关键词" --pages 3 --out data/raw/x.jsonl
python scripts/collect.py --note <note_id> --with-comments --xsec-token <token> --out data/raw/note.jsonl
python scripts/collect.py --user <user_id> --pages 3 --out data/raw/user.jsonl

# 浏览器引擎工具
python scripts/playwright_driver.py --probe        # 检查浏览器/SDK 状态
python scripts/playwright_driver.py --shutdown     # 关闭浏览器单例（pipeline 已自动做）
python scripts/capture_sdk.py                      # 重抓 XHS JS bundle（仅改版时用）
```
