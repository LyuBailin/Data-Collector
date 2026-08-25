---
name: xiaohongshu-harvester
description: 小红书公开/半公开内容采集与本地化分析 skill。能力：关键词搜索笔记、单篇笔记详情 + 评论、用户主页笔记、热门榜、用户搜索，以及 collect→clean→enrich→analyze 全链路，输出洞察式 Markdown 报告 + CSV 明细。适用：用户要求抓取/分析/导出小红书笔记、评论、用户、热门榜数据时使用。前提：用户提供本人账号的登录 cookie。约束：不绕过登录态、不并发不高频、拒绝绕过签名风控或未授权数据的抓取。调用约定：agent 调用 skill 前先与用户讨论主题方向与调研维度；**调研维度不预设、不内置，由 agent 与用户讨论自由设计（每个维度配 3-8 个该社区实际使用的关键词）**；再基于维度生成多组关键词并行采集；skill 不预设任何总结模板，由 agent 基于 desc_plain/评论与用户关心的具体问题生成针对性总结报告，每条结论标注 note_id 与来源 run。
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
| 热门榜 / 热搜 | "热门榜"、"热搜词" | ⚠️ 页面路径已 404, 新入口未逆向 (使用受限) |
| 搜索用户 | "搜一下博主 X" | ✅ 接口直接抓取, 单页 ~20 条 |

## 2. Agent 多轮交互流程 (核心)

**每次接到用户的"调研某个主题"任务时, agent 必须按以下 5 步走**, 不能跳过:

### Step 1 — 与用户讨论主题方向

不要直接挑一个关键词跑。用户原话往往是粗粒度 ("分析小红书秋招"), 需要先和用户确认主题边界:

> 示例问句:
> - "你说的'秋招'是想看整体就业形势, 还是只看大厂?"
> - "调研'护肤品', 偏功效 (美白 / 抗老) 还是偏场景 (军训急救 / 换季维稳)?"

把用户回答提炼成**一句话主题**, 作为下一步维度设计的天花板。

### Step 2 — 与用户讨论调研维度 (维度由 agent 自由设计, 不硬编码)

主题确认后, agent 提出 3–5 个**调研维度**让用户选 (多选)。维度是这个主题下用户想看的角度。

**维度没有预设清单, 由 agent 根据主题与用户讨论自由设计。** 下面是设计规则与示例, 不是内置维度:

**维度设计规则**:
1. **每个维度 = 一个用户关心的角度** (如"品牌推荐" / "避雷" / "搭配技巧"), 维度之间要可区分, 不要互相重叠
2. **每个维度需要 3–8 个关键词**, 关键词必须是**该内容社区实际使用的词** (护肤圈说"烂脸"不说"事故"; 穿搭圈说"版型"不说"款式参数") —— 关键词质量直接决定命中质量
3. **维度名用短 ASCII 标识** (如 `fit` / `brand` / `hc`), 它是 cross_analyze 输出 JSON 的章节键
4. 维度设计完要**给用户看并确认**, 用户有增删改的自由

**示例 (仅示意, 不要照搬)**:

| 主题 | agent 可设计的维度示例 |
| --- | --- |
| 秋招 / 校招 | `hc:缩招,秋招,校招,暑期转正,泡池子,排序` · `interview:面经,面试,一面,反问,上岸` · `company:大厂,字节,阿里,996,裁员,wlb` |
| 露营 | `gear:装备,帐篷,天幕,营地车,卡式炉` · `rookie:新手,踩雷,避雷,漏带` · `safety:危险,隐患,起火,爆炸` |
| 护肤品 | `disaster:烂脸,过敏,红痒,爆痘,激素脸` · `brand:品牌,平价,替代,店铺` · `ingredient:成分,致痘,致敏,敏感肌` |
| 183 胖穿搭 | `fit:穿搭,搭配,显瘦,显高,遮肚,版型` · `brand:大码,微胖,胖男生,品牌,店铺` |

> 关键: 每次调研的维度/关键词都由 agent 当场设计, 不存在"skill 内置了哪些维度"的说法。如果某个维度的关键词是 agent 硬凑的 (比如给护肤话题填"起火/爆炸"), 命中必然为 0, 这是设计错误不是数据问题。

### Step 3 — 生成关键词组合, 让用户确认

基于已选维度, 每个维度生成 1–3 个关键词 (中文优先, 加具体年份 / 限定词提高样本新鲜度), 列出给用户确认 (支持多选)。**展示时每个关键词标注对应维度**, 让用户知道每个关键词在补哪个调研面:

```
主题: 秋招 / 校招
已选维度: hc · interview · company
建议关键词组 (每个标注对应维度):
  [→hc]        hc 缩减, 27 届秋招, 互联网 hc
  [→interview] 面试经验, 秋招面试, 大厂面经
  [→company]   大厂避雷, 大厂工种, 互联网 996
请勾选要跑的组 (默认全跑):
```

**关键词必须明确标注"对应哪个维度"**, 否则用户不知道选 `护肤品烂脸` 是在补"烂脸/翻车"还是"产品对比"维度。

### Step 4 — 并行 / 串行执行多关键词 pipeline

**用 `--keywords` 一个进程跑全部关键词** (推荐, 复用 Chromium + cookie 单例):

```bash
python scripts/pipeline.py \
    --keywords "kw1,kw2,kw3" \
    --pages 1 --enrich-notes 5 --with-comments \
    --workspace data/runs
```

- 逗号分隔多个关键词, **同进程串行** (cookie + Chromium 单例必须复用, 不可并发)
- 每个关键词独立 run folder (topic 从 keyword 自动 slug, `--topic` 在批量模式下被忽略)
- 单关键词失败不会中断整个批次, graceful degradation (跳过该词继续下一个, 整体 rc=1)
- 详细参数见 §5 速查表

**旧写法 (不推荐)**: 也可以 N 次 `pipeline.py --keyword X --topic <slug>`。但每次启动独立进程 = N 次 Chromium 冷启, 仅在必须不同 `--topic` / `--sort` 时才用。

**风控处理**: 如果触风控 (`-102` / `networkidle` 超时), 按 §7 等 90s 重试, 仍失败则跳过该关键词并明示用户。

**Step 4 后产生 N 个 run folder**, 每个含 7 个文件 (`raw.jsonl / clean.jsonl / enriched.jsonl / report.md / summary.json / notes.csv / comments.csv`)。

### Step 5 — 跨 run 聚合 + 针对性总结

单 run 报告 (§5 输出物) 只覆盖单个关键词, **不能用单 run 报告给用户做总结**。

调用 `scripts/cross_analyze.py --runs <slug1>,<slug2>,... --dimensions "<name>:<kw1>,<kw2>;<name2>:<kw3>,<kw4>" --workspace data/runs` 生成聚合 JSON。`--dimensions` 传 Step 2 设计的**完整维度定义** (维度名 + 关键词集), 脚本**没有任何内置维度**, 你传什么它就聚合什么。agent 读聚合 JSON + 各个 `enriched.jsonl`, 基于用户关心的具体问题, 标注 `note_id` 与来源 run, 生成针对性报告。

**新 CLI 示例** (每个维度完整定义, 无内置维度, 无 --custom-dimensions):
```bash
python scripts/cross_analyze.py \
    --runs pants183,daman,pangnansheng,damanpinpai \
    --dimensions "fit:穿搭,搭配,显瘦,显高,遮肚,版型;brand:大码,微胖,胖男生,品牌,店铺,推荐" \
    --workspace data/runs
```
可选调参: `--top-notes N` (默认 10) / `--top-comments N` (默认 10) / `--full-notes N` (默认 5) / `--output <path>` (默认 `<workspace>/_cross_analyze.json`)。

**稀薄维度处理**: 如果某个维度命中笔记数 = 0 (通常是维度关键词设计错误, 例如给护肤话题填"起火/爆炸"), cross_analyze.py 会以**退出码 2** + stderr `WARN: 维度 X 命中稀薄` 告警。Agent 必须:
1. 调整该维度的关键词集 (换成该社区实际使用的词) 后重跑, 或
2. 把该维度从报告里去掉, 并向用户明示"该维度在本次样本里无命中"
3. 不要为了让维度"看起来有内容"就强行归纳

**报告落盘 (可选)**: agent 把写好的针对性总结保存为 `<workspace>/<date>_<topic>_report.md` (或类似的语义化名字), 这样用户后续能直接打开查看历史调研。**不要覆盖**单 run 自己的 `report.md`。

**反面示例 (绝对不能做)**:
- ❌ 跳过 Step 1-3 直接挑关键词跑 ("用户说秋招, 我就只跑 '秋招'")
- ❌ 只跑 1 个关键词就出报告 ("样本不够")
- ❌ 用单 run 的 `report.md` §1 核心结论直接回答用户 (那是通用统计骨架)
- ❌ 不标 `note_id` / 来源 run, 给用户贴一段自己的总结 (违反"信息来源明确"原则)

**正面示例**: 见 §7。

## 3. 开始前检查 (每次任务必做)

1. **Python 环境**: 使用 `data-collect` conda 环境 (Python ≥ 3.10, 推荐 3.12), 依赖装 `pip install -r requirements.txt` (含 `requests` / `jieba` / `playwright`)。首次运行先 `python -m playwright install chromium` (含 chromium-headless-shell, 不要漏)。所有命令在仓库根目录执行。
2. **Cookie**: `assets/cookies.json` 必须存在且有效。
   - 先跑 `python scripts/xhs_client.py whoami` 确认 cookie **格式能解析**(只打印 cookie 摘要, 不打 XHS)
   - 再跑一次**最小真实请求**验证 cookie **真有效**: `python scripts/pipeline.py --keyword "本周热榜" --pages 1 --workspace data/runs/_cookie_test` (完成后 `rm -rf data/runs/_cookie_test`)
   - 失效信号 `-101` / HTTP 401/403 / `code:300011` → **停止**, 请用户重新导出 (Chrome DevTools → Application → Cookies → 导出)
   - cookie 是敏感文件 (`.gitignore` 已排除), **绝不写入 git / 聊天记录**
3. **从用户消息解析参数**:
   - 笔记链接 `https://www.xiaohongshu.com/explore/<note_id>?xsec_token=<token>&...` → 提取 `note_id` 和 `xsec_token` (URL 里 `xsec_token=` 后面的值)
   - 用户主页链接 `https://www.xiaohongshu.com/user/profile/<user_id>` → 提取 `user_id`

## 4. 标准操作 (按用户意图选命令)

### 4.1 关键词分析 (单关键词, 适用于用户已锁定特定词)
```bash
# 完整分析 (推荐): 搜索 + Top10 高互动笔记补全正文/标签/时间 + 评论
python scripts/pipeline.py --keyword "关键词" --pages 3 --enrich-notes 10 --with-comments --workspace data/runs

# 只要搜索 + 统计, 不补全 (快, 但报告缺正文维度)
python scripts/pipeline.py --keyword "关键词" --pages 3 --workspace data/runs
```

### 4.2 多关键词调研 (推荐路径, 与 §2 Step 4 对应)
```bash
# 一个进程串行跑多个关键词 (推荐, Chromium/cookie 单例复用)
python scripts/pipeline.py \
    --keywords "hc 缩减,面试经验,大厂避雷" \
    --pages 1 --enrich-notes 5 --with-comments \
    --workspace data/runs

# 跑完后跨 run 聚合 (--dimensions 必须用 name:kw1,kw2;name2:kw3,kw4 格式, 维度由 agent 设计)
python scripts/cross_analyze.py \
    --runs "hc 缩减,面试经验,大厂避雷" \
    --dimensions "hc:hc,缩招,秋招,校招,暑期转正;interview:面经,面试,上岸;company:大厂,字节,996,裁员" \
    --workspace data/runs
```

> **注意**:`--runs` 的 slug 必须与 `--keywords` 中的关键词**字面一致**(脚本按 `_slugify(keyword)` 命名 run folder, 中文标点/空格保留, 简体原样)。如果关键词含逗号导致 `--runs` 转义复杂, 先跑一次 `--keywords` 看实际生成的 folder 名(形如 `2026-08-25_hc 缩减/`), 再把这些名字拼到 `--runs`。

### 4.3 单篇笔记 + 评论
```bash
python scripts/pipeline.py --note <note_id> --with-comments --xsec-token <token> --workspace data/runs
```
`xsec_token` 必须从笔记 URL 复制; 缺失可能拿不到正文或评论。

### 4.4 用户主页笔记
```bash
python scripts/pipeline.py --user <user_id> --pages 2 --workspace data/runs
```

### 4.5 只要原始数据 (不要报告)
```bash
python scripts/collect.py --keyword "关键词" --pages 3 --out data/raw/x.jsonl
```

### 4.6 分阶段调试
```bash
python scripts/collect.py --keyword "关键词" --pages 3 --out data/raw/x.jsonl
python scripts/clean.py   --in data/raw/x.jsonl --out data/clean/x.jsonl
python scripts/enrich.py  --in data/clean/x.jsonl --out data/enriched/x.jsonl
python scripts/analyze.py --in data/enriched/x.jsonl --report x.report.md --summary x.summary.json
```

## 5. 参数速查

| 参数 | 适用模式 | 说明 |
| --- | --- | --- |
| `--keyword` / `--pages` | keyword | 单关键词与页数 (默认 3 页 ≈ 60 条; 搜索卡片无正文/时间戳) |
| `--keywords "k1,k2,..."` | keyword (批量) | 多关键词 (逗号分隔), 同进程串行跑, 复用 Chromium/cookie 单例; 每个关键词独立 run folder; 单关键词失败不中断批次 |
| `--enrich-notes N` | keyword | 对热度 Top N 补全正文/标签/时间戳 (推荐 10; 多关键词场景推荐 5) |
| `--with-comments` | keyword/note | 同时抓评论 (补全时评论来自同一页面导航, 不额外耗请求) |
| `--note` / `--user` / `--hotlist` / `--search-user` | 模式 | 互斥, 必须且只能选一个 |
| `--xsec-token` | note | 笔记访问令牌 |
| `--topic` | 单 keyword / user / note / hotlist | run folder 名字后缀, 默认由 keyword/user/note 推断; **批量模式 (`--keywords`) 下被忽略**, topic 自动从 keyword 派生 |
| `--workspace` | 全部 (仅 pipeline.py) | 输出根目录 (默认 `data/runs`, 每次新建 `<日期>_<topic>` 子目录) |
| `--sign-engine` | 全部 | 默认 `browser` (页面驱动); `node`/`legacy` 仅供调试, 当前 XHS 拒绝 |
| `--category` | hotlist | 热门榜分类, 默认 `general` |
| `--page-size` | keyword / hotlist | 单页笔记/热门词条数, 默认 20 / 50 |
| `--sort` | keyword | 搜索排序: `general` (默认) / `popular` / `time_descending` |
| `--max-comment-pages` | note (with --with-comments) | 评论翻页数, 默认 3 |

## 6. 输出物 (run folder)

每个 pipeline run 产生 7 个文件:

| 文件 | 内容 |
| --- | --- |
| `raw.jsonl` / `clean.jsonl` / `enriched.jsonl` | collect / clean / enrich 各阶段数据 |
| `report.md` | 通用报告骨架: 数据质量 / 互动热度分布 / 关键词 / 话题×互动 / 情感×互动 / Top 笔记 (正文摘要) / Top 用户 / 评论分析 / 风险 |
| `summary.json` | 机器可读统计 |
| `notes.csv` / `comments.csv` | Excel 可直接打开的明细 (UTF-8 BOM) |

**重要**: 单 run 的 `report.md` **不能作为最终答案**给用户, 只是数据骨架。详见 §7。

## 7. 故障排查 (Agent 决策表)

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 搜索返回 `code:300011` | 旧 v1 接口废弃 (内置已是 v2 页面驱动) | 无需处理; 若仍出现, 确认没被手动切成 node/legacy 引擎 |
| `HTTP 406` / `Failed to fetch` | raw fetch 被网关拒 / 页面 fetch 包装偶发 | 走页面驱动路径; 降低频率, 等几分钟重试 |
| `-101` / HTTP 401/403 | cookie 失效 | **停止**, 请用户重新导出 cookie |
| `-102` / 连续空数据 / `networkidle` 60s 超时 | 风控触发 | 等 90s 重试 1 次, 仍失败则跳过该关键词并明示用户 |
| 笔记详情拿不到正文 | `xsec_token` 缺失/失效 | 从笔记 URL 复制 xsec_token 重试 |
| 报告缺正文/话题/时间分布 | v2 搜索卡片不含正文 (接口限制) | 加 `--enrich-notes N` 补全 Top 笔记 |
| 用户主页只有少量笔记 | SSR 首屏 + 滚动加载, 或该用户笔记少 | `--pages` 触发滚动; 数据量本身受页面限制 |
| `--hotlist` 报错 / 空数据 | 热门榜页面驱动触发风控或路径变更 | 等几分钟降频重试; 持续失败再排查页面路径 |
| 多关键词跑时被中断 | 第 N 个关键词触发风控 | 后续关键词的 run folder 会自动加 `_1` `_2` 后缀, 不影响 |
| `cross_analyze.py` 参数解析失败 (exit 3) | `--dimensions` 格式不对 (缺冒号 / 空关键词 / 维度名重复) | 按报错信息修正 `name:kw1,kw2;name2:kw3,kw4` 格式 |
| `cross_analyze.py` 退出码 2 + `WARN: 维度 X 命中稀薄` | 该维度在本次样本里 0 笔记命中 (通常是关键词设计错误, 词不贴社区) | 换成该社区实际使用的关键词重跑, 或从报告里去掉该维度并明示用户 |
| `cross_analyze.py` 报 `WARN: slug 'X' 在 ... 下无匹配 run folder` | `--runs` 的 slug 与 `--keywords` 拼写不一致, 或对应关键词 pipeline 没跑过 | 检查 `data/runs/` 实际 folder 名, 把 `--runs` 改成与之字面一致; 同 slug 多副本时会显式列出所有候选 + 选了哪个 |

## 8. Agent 后续处理契约 (基于 desc_plain / content 写报告)

`report.md` §1 核心结论是通用统计骨架, **不替代给用户的最终答案**。Agent 在多关键词跑完后 (§2 Step 5) 按以下步骤产出针对性报告:

1. **读 `cross_analyze.py` 输出的聚合 JSON** (默认 `<workspace>/_cross_analyze.json`) — 它按维度聚合了所有 run 的笔记 / 评论
2. **报告必须覆盖所有用户已选维度** (`by_dimension` 里的所有 key), 即使某个维度数据少, 也要明示稀薄; **不允许 agent 挑维度报告**
3. **针对每个调研维度, 选 Top 笔记 + Top 评论** (聚合 JSON 里已按赞数排好)
4. **对于有 `desc_plain` 的笔记 (聚合 JSON `full_notes` 字段), 直接引用原文** — 不要改写、不要概括、不要创作
5. **对于评论, 直接引用 `content` 原文**, 标 `run` + `note_id` + `comment_id` (不是 note_id, 而是评论自身的 id) + `user` (匿名评论标 "匿名")
6. **每个结论至少 1 条证据**, 不允许"我认为" / "感觉上" / "用户普遍觉得..." 类无源断言
7. **每条评论引用必须标 `comment_id`**, 不能只说"评论区有人提到..." 这是契约里最容易违反的点: 评论里的金句必须能溯源到具体 comment_id
8. **如果维度数据稀薄** (聚合 JSON 里某维度命中笔记数 < 3), 在报告里明示并建议补跑关键词

**反面示例**:
- ❌ 把单 run 的 `report.md` §1 直接转给用户 ("主题集中度最高频关键词..."; 通用骨架, 不是针对性总结)
- ❌ 只贴点赞量/话题统计 (用户已经反馈过这没意义)
- ❌ 只说 "完整正文见 report.md" (等于没总结)
- ❌ 自己总结出"小红书用户普遍觉得...", 找不到具体 note_id 来源
- ❌ 报告只覆盖 3 个维度里的 2 个, 跳过稀薄的那个 (违反 §8 步 2)
- ❌ 引用评论时说"评论区有人提到 X", 没标 comment_id (违反 §8 步 7)

**正面示例** (来自真实秋招调研):
> "**HC 缩减情况**:根据 `hcsuojian/run` Calvin 在大厂《27届校招将是互联网最难的一年》(note `6a2fe20b`, 813 赞, 已补全正文 9 家大厂 HC 数据):
> - 京东: 采销去年 4000+, 今年预计只有去年一半, 大部分需暑期转正
> - 美团: 非技术岗预计去年 50%
> - 小红书: 少数继续扩招的大厂, 总量 200+
>
> 评论区佐证: `hcsuojian/run` note `6a852bc4`, comment_id `6a8543f5`, @We1L (46 赞) 提到 '今年秋招体感温度非常低, 阿里云也是煎熬'..."

## 9. 红线与合规 (必须遵守)

- 仅使用用户本人账号的 cookie; **不绕过登录态、不绕过签名风控**
- 默认限速 1–2s/请求, 抓取 ≤3 页, 不并发
- 一旦出现 `-101` / 登录失效 / 账号冻结信号, 立即停止并提示, 不自动重试打爆接口
- cookie 不入 git / 聊天记录 / 共享位置; 数据仅供用户本地分析

## 10. 命令速查 (完整)

```bash
conda activate data-collect
python -m playwright install chromium   # 首次 (含 chromium-headless-shell)

# cookie 检查 / 加载
python scripts/xhs_client.py whoami
python scripts/xhs_client.py init-cookie

# 多关键词调研 (§2 推荐路径, 一次进程跑全部)
python scripts/pipeline.py \
    --keywords "183 胖穿搭,大码男装,胖男生穿搭" \
    --pages 1 --enrich-notes 5 --with-comments \
    --workspace data/runs
# 跨 run 聚合: 维度由 agent 设计, 无内置维度; --runs 用关键词字面 (与 --keywords 对应)
python scripts/cross_analyze.py \
    --runs "183 胖穿搭,大码男装,胖男生穿搭" \
    --dimensions "fit:穿搭,搭配,显瘦,显高,遮肚,版型;brand:大码,微胖,胖男生,品牌,店铺" \
    --workspace data/runs

# 单关键词 (用户明确指定了某个词)
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
python scripts/playwright_driver.py --shutdown     # 关闭浏览器单例 (pipeline 已自动做)
python scripts/capture_sdk.py                      # 重抓 XHS JS bundle (仅改版时用)
```