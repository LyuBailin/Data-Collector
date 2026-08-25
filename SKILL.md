---
name: xiaohongshu-harvester
description: 小红书内容采集 + 本地分析。用户提供本人 cookie 后, agent 与用户讨论主题与调研维度(维度由 agent 自由设计, 不预设; 每维度 3-8 个该社区实际使用的关键词), 用 --keywords 批量跑多关键词(同进程串行, 复用 Chromium/cookie 单例), 用 cross_analyze.py 按维度聚合, agent 基于 desc_plain 与评论写报告(每条标 note_id / comment_id / 来源 run)。约束: 不绕过登录态 / 签名风控 / 未授权数据。
---

# 小红书采集分析

## 核心流程(5 步, 不能跳过)

1. **讨论主题 + 设计维度** — 与用户明确主题边界。agent 自由设计 3-5 个调研维度, **每个维度 = 一个用户关心的角度**, **3-8 个关键词必须是该内容社区实际使用的词** (护肤圈说"烂脸"不说"事故"; 穿搭圈说"版型"不说"款式参数")。**维度名 = 短 ASCII 标识** (` hc` / `fit` / `brand`), 它是 `cross_analyze.py --dimensions` 的章节键。
2. **生成关键词组 + 用户确认** — 每个关键词标注对应维度 (` [→hc] hc 缩减, 27 届秋招`), 否则用户不知道 `护肤品烂脸` 在补"烂脸/翻车"还是"产品对比"。
3. **批量跑 pipeline**:
   ```bash
   python scripts/pipeline.py --keywords "kw1,kw2,kw3" \
       --pages 1 --enrich-notes 5 --with-comments --workspace data/runs
   ```
   同一进程串行, 复用 Chromium/cookie 单例 (cookie 是敏感文件, .gitignore 已排除)。输出 N 个 `data/runs/<日期>_<topic>/`, 每文件夹 7 个文件。
4. **跨 run 聚合**:
   ```bash
   python scripts/cross_analyze.py --runs "kw1,kw2,kw3" \
       --dimensions "name:kw1,kw2;name2:kw3,kw4" --workspace data/runs
   ```
   生成 `_cross_analyze.json`, 按维度聚合笔记 + 评论。无内置维度, 你传什么它聚合什么。
5. **写报告** — 读聚合 JSON + 各 `enriched.jsonl`, 基于 `desc_plain` / 评论原文回答用户问题, 落盘为 `<workspace>/<date>_<topic>_report.md`。

## 必做检查

1. `pip install -r requirements.txt` + `python -m playwright install chromium` (含 chromium-headless-shell)
2. `python scripts/xhs_client.py whoami` 验证 cookie 文件能解析 (只解析, 不打 XHS)
3. 跑一次 `pipeline.py --keyword <tiny_term> --pages 1 --workspace data/runs/_cktest` 验证 cookie **真有效** (完成后 `rm -rf data/runs/_cktest`)
4. cookie 缺失 / 错误信息含 "**页面渲染了登录墙, cookie 可能已过期**" → **立即停止**, 让用户重新导出 (Chrome DevTools → Application → Cookies)

## 单模式命令(用户给明确目标时)

```bash
python scripts/pipeline.py --keyword "X"            --pages 3 --workspace data/runs              # 单关键词
python scripts/pipeline.py --note <id> --xsec-token <token> --with-comments --workspace data/runs # 单笔记+评论
python scripts/pipeline.py --user <id> --pages 2 --workspace data/runs                              # 用户主页
python scripts/pipeline.py --hotlist --category general --workspace data/runs                        # 热门榜 (频率敏感)
python scripts/pipeline.py --search-user "X" --pages 1 --workspace data/runs                        # 搜博主
```

## 报告契约(必须遵守)

- 每条结论至少 1 条 `note_id` 或 `comment_id` 证据
- 评论金句必须标 `comment_id`, 不允许 "评论区有人提到..."
- 覆盖 `by_dimension` 全部 key, **稀薄维度也要明示** (不允许挑维度报告)
- 直接引用 `desc_plain` / 评论原文, **不改写、不概括、不创作**
- 单 run 的 `report.md` §1 是通用统计骨架, **不能复制给用户**

## 故障排查(主要症状)

| 症状 | 处理 |
|---|---|
| "页面渲染了登录墙, cookie 可能已过期" | 立即停, 让用户重导 cookie (不是风控, 不要等 90s 重试) |
| `-102` / 连续空数据 | 等 90s 重试, 仍失败跳过该关键词 |
| `cross_analyze.py exit 3` | `--dimensions` 格式错, 按 `name:kw1,kw2;name2:kw3,kw4` 修正 |
| `cross_analyze.py exit 2` + `WARN: 维度 X 命中稀薄` | 通常是关键词设计错误, 换该社区实际用的词, 或从报告去掉该维度并明示 |
| `WARN: slug 'X' 无匹配 run folder` | `--runs` 拼写错, 或对应 pipeline 没跑过 |

## 红线

仅用户本人 cookie; 不绕过登录态 / 签名风控; 默认 1-2s/请求, 不并发; cookie 不入 git / 聊天记录 / 共享位置。

## 详细参考(给开发者)

- 命令参数全表: `python scripts/pipeline.py --help` / `python scripts/cross_analyze.py --help`
- 输出字段定义: [`references/output_schema.md`](references/output_schema.md)
- 接口实测: [`references/api.md`](references/api.md)
- Cookie 维护: [`references/cookie.md`](references/cookie.md)
- 签名机制: [`references/signing.md`](references/signing.md)