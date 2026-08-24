---
name: xiaohongshu-harvester
description: 小红书 (xiaohongshu.com) 公开 / 半公开数据采集、清洗、增强与汇总分析 skill。覆盖关键词搜索笔记 / 用户主页笔记 / 单篇笔记详情 / 热门榜 / 评论等接口，提供基于 Playwright 的浏览器内签名、HTML/Markdown 清洗、话题与关键词增强、互动分布与词频报告。适用于需要离线本地化分析小红书内容的场景；当用户要求抓取 / 分析 / 导出小红书帖子、用户、热门榜或评论数据时使用本 skill。当用户希望绕过登录态抓取或绕过签名风控时拒绝执行。
---

# 小红书数据采集 / 清洗 / 增强 / 汇总分析 (xiaohongshu-harvester)

本 skill 提供一条端到端 pipeline, 在用户提供的 cookie 上下文下完成小红书信息的本地化处理:

```
collect  ->  clean  ->  enrich  ->  analyze
   raw        clean      enriched     report.md / summary.json
```

## 状态

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| clean | ✅ 已验证 | 8 条 mock 数据端到端通过 |
| enrich | ✅ 已验证 | jieba + 情感词典 + 热度 + 广告启发式 |
| analyze | ✅ 已验证 | summary.json + report.md |
| 采集 HTTP 客户端 | ✅ 已验证 | Playwright 浏览器引擎; 关键词搜索走**页面驱动** (打开搜索页拦截 v2 search 响应), 笔记详情/评论走笔记页 `__INITIAL_STATE__` + 页面评论响应 |
| 签名 (X-s/X-t) | ✅ 已验证 | 浏览器内 `seccore_signv2` 跑通, 服务器返回 200 + 真实 JSON; raw fetch 对 so 搜索网关会 406, 已改页面驱动 |

> **勘误 (2026-08)**: 之前记录的 `code:300011` 账号异常**并非账号风控** —— 那是旧版
> `edith.../v1/search/notes` 接口废弃后的拒答。用户账号本身正常 (页面自身请求全部 `code:0`)。
> 修复方式是把搜索切到 `so.xiaohongshu.com/api/sns/web/v2/search/notes` + 页面驱动采集。

## 使用前提

* 已登录小红书的 Chrome, 用 DevTools / Cookie-Editor 导出 cookie 到 `assets/cookies.json` (格式见 `references/cookie.md`)。
* 已创建 `data-collect` conda 环境并安装 `requests` / `jieba` / `beautifulsoup4` / `playwright`。
* 第一次运行前执行 `python -m playwright install chromium` (下载 Chromium)。
* 接受 XHS 反爬约束: **不绕过登录态, 不并发, 不高频**, 失败时停止并提示重新提供 cookie。

## 端到端流程

1. **加载 cookie**: `python scripts/xhs_client.py init-cookie`
2. **采集 (collect)**: `python scripts/collect.py --keyword "露营" --pages 3 --out data/raw/search_露营.jsonl`
3. **清洗 (clean)**: `python scripts/clean.py --in data/raw/...jsonl --out data/clean/...jsonl`
4. **增强 (enrich)**: `python scripts/enrich.py --in data/clean/...jsonl --out data/enriched/...jsonl`
5. **汇总分析 (analyze)**: `python scripts/analyze.py --in data/enriched/...jsonl --report report.md --summary summary.json`
6. **一键**: `python scripts/pipeline.py --keyword "露营" --pages 3 --workspace data/`

## 关键约束

### Cookie 与登录态
- 仅使用用户自己账号的 cookie, 不要把 cookie 写入 git / 共享位置 / 聊天记录。
- 一旦 `code = -101` / `login_required` / `account.frozen` / HTTP 401/403 / `code = 300011` (账号状态异常), 必须立刻停止并提示重新提供 cookie。
- 详细字段含义、维护流程、失效信号见 `references/cookie.md`。

### X-s / X-t 签名
- 默认签名引擎 `--sign-engine browser` 通过 Playwright 启动 Chromium, 注入 cookie, 加载 `xiaohongshu.com` 触发所有静态 bundle + 动态 SDK (`window.mnsv2`), 然后 `page.evaluate(fetch(url, ...))` 发请求。浏览器自己的 axios 拦截器自动加 `X-s` / `X-t` / `X-common-params` / `X-mns` / `X-web-s` / `X-web-t`。
- **注意 (2026-08 实测)**: 关键词搜索接口已迁移到 `so.xiaohongshu.com/api/sns/web/v2/search/notes`。该网关会校验页面 axios 完整链路附加的 `x-s-common` 等额外签名头, **raw fetch 会被 406 拒绝**; 旧 v1 接口在 edith 上返回 `code:300011` (废弃拒答, 不是账号风控)。因此浏览器引擎对搜索 / 笔记详情 / 评论统一改为**页面驱动**: 打开搜索页 / 笔记页, 拦截页面自身发出的 API 响应 (`scripts/playwright_driver.py::page_search_notes / page_note_detail`)。响应字段差异见 `references/api.md`。
- `--sign-engine node` 和 `--sign-engine legacy` 是为离线调试 / 教学保留, 当前 XHS web 上不会被服务端接受 (签名算法已经被 seccore_signv2 替换)。
- 签名引擎的所有细节见 `references/signing.md`。

### 限速 / 风控
- 默认每次请求 `1.0–2.0s` 随机 jitter; 抓取深度默认 ≤3 页。
- 浏览器引擎每次抓取会复用同一个 `BrowserContext` (避免重复启动 Chromium)。`pipeline.py` / `collect.py` 结束时自动关闭浏览器单例; 长驻进程里也可手动执行 `python scripts/playwright_driver.py --shutdown`。
- 连续 5 次空数据 / 风控 `-102` 时暂停 60s 重试 1 次, 仍失败则退出。

## 参考资源

- `references/cookie.md` — cookie 字段、维护流程、安全提示。
- `references/api.md` — 接口端点、参数、签名约定摘要。
- `references/signing.md` — X-s/X-t 签名机制详解 + 浏览器引擎原理。
- `references/output_schema.md` — 每个阶段 JSONL / JSON / MD 字段定义。
- `scripts/` — 可独立运行的 Python 脚本 (采集 / 清洗 / 增强 / 分析)。
- `scripts/playwright_driver.py` — 浏览器引擎, 单例 BrowserContext。
- `scripts/capture_sdk.py` — 用 Playwright 抓 XHS 首页 JS bundle (一次性, 已生成结果)。
- `assets/bundles/` — XHS 首页 JS bundle + 动态 SDK (`.gitignore` 排除可重建)。

## 命令速查

```bash
# 0) 准备
conda activate data-collect
python -m playwright install chromium

# 1) 加载 cookie
python scripts/xhs_client.py init-cookie

# 2) 一键 pipeline (关键词搜索 + 整链路分析, 默认 browser 引擎)
python scripts/pipeline.py --keyword "露营装备" --pages 3 --workspace data/

# 3) 分阶段
python scripts/collect.py --keyword "露营装备" --pages 3 --out data/raw/search.jsonl
python scripts/clean.py   --in  data/raw/search.jsonl  --out data/clean/search.jsonl
python scripts/enrich.py  --in  data/clean/search.jsonl --out data/enriched/search.jsonl
python scripts/analyze.py --in  data/enriched/search.jsonl \
                          --report data/search.report.md --summary data/search.summary.json

# 4) 切换签名引擎 (默认 browser)
python scripts/collect.py --sign-engine node --keyword "test" --pages 1 --out /tmp/x.jsonl  # 调试用, 当前 XHS 拒绝
python scripts/collect.py --sign-engine legacy --keyword "test" --pages 1 --out /tmp/x.jsonl  # 调试用, 当前 XHS 拒绝

# 5) 用户主页
python scripts/collect.py --user <user_id> --pages 3 --out data/raw/user.jsonl

# 6) 单篇笔记 + 评论 (浏览器引擎: 需要 xsec_token, 从笔记 URL 复制)
python scripts/collect.py --note <note_id> --with-comments --xsec-token <xsec_token> --out data/raw/note.jsonl
python scripts/pipeline.py --note <note_id> --with-comments --xsec-token <xsec_token> --workspace data/runs

# 7) 热门榜
python scripts/collect.py --hotlist --out data/raw/hotlist.jsonl

# 8) 浏览器引擎直接探针 / 自定义请求
python scripts/playwright_driver.py --probe
python scripts/playwright_driver.py --request '{"method":"GET","url":"/api/sns/web/v1/search/trending/list","headers":{}}'

# 9) 关闭浏览器进程 (结束 / 长驻进程手动关闭)
python scripts/playwright_driver.py --shutdown
```

## 已验证

* `clean / enrich / analyze` 三阶段在 mock 数据 (`assets/sample_data.jsonl`) 上端到端跑通, 报告含 8 条记录的关键词、互动率、热度榜、用户聚合、广告样本。
* `xhs_client.py init-cookie / whoami` 在 cookie 文件存在时正常解析。
* `scripts/playwright_driver.py` 启动 Chromium, 加载 cookie, 触发 XHS 静态 + 动态脚本, 实际发请求 → 服务器返回 200 + JSON。
* `scripts/capture_sdk.py` 抓取 XHS 首页 JS bundle 到 `assets/bundles/`, 包括动态 SDK `sdk.js` (定义了 `window.mnsv2`)。
* `pipeline.py` 串行调度各阶段。

## 注意事项

1. **Cookie 状态**: 浏览器引擎能成功发送签名请求, 但 XHS 服务端返回的 `code` 是基于账号本身的状态。`code:300011` 通常表示账号被风控, 需要换用新 cookie。`code:0` 表示成功。
2. **API host**: XHS 真正的 API 服务器是 `edith.xiaohongshu.com`, 不是 `www.xiaohongshu.com`。`scripts/playwright_driver.py` 自动处理这个重定向 (相对 URL `/api/...` 在浏览器里会被自动解析到 `edith.xiaohongshu.com`)。
3. **首次启动慢**: Playwright 第一次启动会下载 Chromium (~150MB)。之后启动约 2-3 秒, 后续抓取每次约 1-3 秒 (受 XHS 限速)。
4. **沙箱粘性**: `playwright_driver.py` 把浏览器做成单例, 复用同一个 `BrowserContext`。第二次抓取跳过 Chromium 启动, 秒级响应。
