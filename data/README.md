# data/ — 数据目录结构

每次 collect → clean → enrich → analyze 跑下来,会生成一个 **run folder** 放在 `data/runs/` 下:

```
data/
└── runs/
    ├── sample/                              <- 教学用 mock, 提交到 git
    │   ├── raw.jsonl / clean.jsonl / enriched.jsonl
    │   ├── report.md / summary.json
    │   └── notes.csv
    │
    └── <date>_<topic-slug>/                 <- 实际跑出来的 run folder
        └── 7 个文件 (见下表)
```

## 命名规则

`runs/<YYYY-MM-DD>_<topic-slug>/`

- `<YYYY-MM-DD>`: 抓取日期
- `<topic-slug>`: 主题短描述, 全 ASCII + 下划线

每个 run folder 默认生成 **7 个文件**:

| 文件 | 是否总有 | 内容 |
| --- | --- | --- |
| `raw.jsonl` | 是 | collect.py 原始抓取 (未经清洗) |
| `clean.jsonl` | 是 | clean.py 处理后 (去 emoji/HTML, 互动率, 字数) |
| `enriched.jsonl` | 是 | enrich.py 处理后 (关键词, 情感, 热度, ad_like) |
| `report.md` | 是 | 给人读的 Markdown 报告 |
| `summary.json` | 是 | 给程序读的统计 |
| `notes.csv` | 是 | Excel 可直接打开的笔记明细 (UTF-8 BOM) |
| `comments.csv` | 仅在有评论记录时 | Excel 可直接打开的评论明细 (UTF-8 BOM) |

## 跑一次

```bash
# 一行命令 = collect → clean → enrich → analyze
python scripts/pipeline.py \
    --note 6a2fe20b000000001702b80f \
    --with-comments \
    --topic 27jiqiuzhao_real \
    --workspace data/runs
```

会创建 `data/runs/<today>_27jiqiuzhao_real/` 包含 5 个 JSON/JSONL/MD 文件 + `notes.csv` + (有评论时) `comments.csv`。

## 提交策略

- `runs/sample/`: 入 git (教学示例)
- `runs/<其它>/`: 不入 git (.gitignore 已经忽略 `data/runs/`)
