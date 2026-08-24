# 输出 Schema

所有阶段都使用 JSONL（一行一条记录），方便流式处理。

## `data/raw/*.jsonl`（采集阶段）

```json
{
  "endpoint": "search/notes | user/posted | feed | comment | hotlist",
  "fetched_at": "2026-08-24T01:13:30+08:00",
  "item": {
    "note_id": "abc123",
    "title": "...",
    "desc": "...",
    "type": "normal | video",
    "user": {"user_id": "...", "nickname": "...", "fans": 1234},
    "interact": {"liked": 100, "collected": 50, "comment": 30, "share": 10},
    "cover": {"url": "...", "width": 1080, "height": 1440},
    "tags": ["#露营", "#装备"],
    "ts": 1716591234000,
    "ip_location": "北京"
  },
  "raw": { ... 原始字段 ... }
}
```

## `data/clean/*.jsonl`（清洗阶段）

```json
{
  "note_id": "abc123",
  "title": "...",
  "desc_plain": "...",      // 去 emoji / 去 HTML / 去 hashtag split
  "tags": ["露营", "装备"],  // 去除 #
  "ts_iso": "2024-05-24T18:00:00+08:00",
  "user": {...},
  "interact": {...},
  "engagement_rate": 0.0823,
  "word_count": 320
}
```

## `data/enriched/*.jsonl`（增强阶段）

在 clean 基础上追加：

```json
{
  "keywords": ["露营", "帐篷", "轻量化"],
  "topics": ["露营装备", "周末出游"],
  "sentiment": "positive | neutral | negative",
  "sentiment_score": 0.61,
  "embedding_summary": "轻量化露营帐篷评测，特点是...",
  "score": 0.78      // 内部热度打分 = 互动率 + 时效 + 完读
}
```

## `summary.json`

```json
{
  "generated_at": "...",
  "source": "data/enriched/xxx.jsonl",
  "total": 200,
  "valid": 195,
  "users": 142,
  "by_type": {"normal": 170, "video": 25},
  "engagement": {"mean": 0.07, "p50": 0.05, "p90": 0.21},
  "top_keywords": [{"word": "露营", "freq": 23}, ...],
  "top_topics": [...],
  "top_users": [...],
  "top_notes": [...]
}
```

## `report.md`

由 `scripts/analyze.py` 生成，包含：

1. 概览（数据量、时间分布、来源、关键指标）
2. 互动分布（直方图数据 + 关键百分位）
3. 关键词 / 话题 Top 20 表格
4. 头部笔记 Top 10（标题、互动率、话题、摘要）
5. 头部用户 Top 10
6. 异常 / 风险样本（疑似广告、低质、低活跃）
7. 原始数据与脚本版本备注
