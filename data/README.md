# data/ — 数据目录结构

每次 collect → clean → enrich → analyze 跑下来,会生成一个 **run folder** 放在 `data/runs/` 下:

```
data/
└── runs/
    ├── sample/                              <- 教学用 mock, 提交到 git
    │   ├── raw.jsonl                         <- 8 条 mock 笔记
    │   ├── clean.jsonl
    │   ├── enriched.jsonl
    │   ├── report.md
    │   └── summary.json
    │
    ├── 2026-08-24_27jiqiuzhao_mock/         <- 27 届秋招 hc 缩减话题的 mock 数据跑
    │   └── raw.jsonl / clean / enriched / report.md / summary.json
    │
    └── 2026-08-24_27jiqiuzhao_real/         <- 真实笔记 (Calvin 在大厂) + 10 条评论
        └── ...
```

## 命名规则

`runs/<YYYY-MM-DD>_<topic-slug>/`

- `<YYYY-MM-DD>`: 抓取日期
- `<topic-slug>`: 主题短描述, 全 ASCII + 下划线

每个 run folder 里有 **5 个固定文件**:

| 文件 | 内容 |
| --- | --- |
| `raw.jsonl` | collect.py 原始抓取 (未经清洗) |
| `clean.jsonl` | clean.py 处理后 (去 emoji/HTML, 互动率, 字数) |
| `enriched.jsonl` | enrich.py 处理后 (关键词, 情感, 热度, ad_like) |
| `report.md` | 给人读的 Markdown 报告 |
| `summary.json` | 给程序读的统计 |

## 跑一次

```bash
# 一行命令 = collect → clean → enrich → analyze
python scripts/pipeline.py \
    --note 6a2fe20b000000001702b80f \
    --with-comments \
    --topic 27jiqiuzhao_real \
    --workspace data/runs
```

会创建 `data/runs/<today>_27jiqiuzhao_real/` 包含 5 个文件。

## 提交策略

- `runs/sample/`: 入 git (教学示例)
- `runs/<其它>/`: 不入 git (.gitignore 已经忽略 `data/runs/`)
