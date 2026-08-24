# assets/ 目录说明

## 运行时数据

- `cookies.json` — 用户登录态, 由 `EditThisCookie` / Chrome DevTools 导出后覆盖写入。本仓库的版本来自用户提供的示例 (16 个 cookie, 包含 `web_session` / `a1` / `id_token` 等)。
- 数据样本 → 已迁移到 `data/runs/sample/` (raw.jsonl / clean.jsonl / enriched.jsonl / report.md / summary.json)

## 抓取 SDK 缓存 (用于 node 签名桥,可重新生成)

- `bundles/` — XHS 首页 JS bundle + 动态 SDK (`sdk.js` 定义 `window.mnsv2`)。改版后跑 `python scripts/capture_sdk.py` 重新抓取。
