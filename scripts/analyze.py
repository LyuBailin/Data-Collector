# -*- coding: utf-8 -*-
"""
analyze.py - 汇总分析与报告生成

输入: data/enriched/*.jsonl
输出:
  - summary.json: 机器可读的统计摘要
  - report.md:    人读报告 (概览 / 互动分布 / 关键词 / 话题 / 头部笔记 / 头部用户 / 异常样本)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("analyze")


def _read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                LOG.warning("JSON 解析失败: %s", exc)
    return rows


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(text)


def _percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] + (values[c] - values[f]) * (k - f)


def _is_note(rec):
    ep = rec.get("endpoint") or ""
    return ep.startswith("search/notes") or ep.startswith("user/posted") or ep.startswith("feed/")


def _is_comment(rec):
    return rec.get("endpoint") == "comment/page"


def _is_hotlist(rec):
    return rec.get("endpoint") == "search/hotlist"


def aggregate(records):
    """汇总统计: 把 note + comment 分开,各自出指标。

    输出字段分类:
      - 整体: total, notes, comments, hotlist_items
      - 笔记维度: by_type, engagement, heat, top_keywords, top_topics, by_day, top_notes, top_users, flagged
      - 评论维度: comment_sentiment, comment_top_keywords, top_commenters, top_comments, sub_comment_count
    """
    notes = [r for r in records if not r.get("is_comment") and not r.get("is_hotlist") and not r.get("is_user")]
    comments = [r for r in records if r.get("is_comment")]
    hotlist = [r for r in records if r.get("is_hotlist")]
    users_search = [r for r in records if r.get("is_user")]

    # ---- 用户维度 (笔记 + 评论都聚合) ----
    users = {}
    for n in notes:
        u = (n.get("user") or {})
        uid = u.get("user_id")
        if not uid:
            continue
        users.setdefault(uid, {
            "user_id": uid,
            "nickname": u.get("nickname") or "",
            "notes": 0,
            "comments": 0,
            "total_liked": 0,
            "total_collected": 0,
            "total_comment": 0,
            "fans": int(u.get("fans") or 0),
        })
        row = users[uid]
        row["notes"] += 1
        row["total_liked"] += int((n.get("interact") or {}).get("liked") or 0)
        row["total_collected"] += int((n.get("interact") or {}).get("collected") or 0)
        row["total_comment"] += int((n.get("interact") or {}).get("comment") or 0)
        if not row["nickname"]:
            row["nickname"] = u.get("nickname") or ""

    er_values = [float(n.get("engagement_rate") or 0.0) for n in notes]
    heat_values = [float(n.get("heat_score") or 0.0) for n in notes]
    type_counter = Counter(n.get("type") or "normal" for n in notes)
    sentiment_counter = Counter(n.get("sentiment") or "neutral" for n in notes)

    # ---- 覆盖度 ----
    coverage = {
        "notes": len(notes),
        "with_desc": sum(1 for n in notes if (n.get("desc_plain") or "").strip()),
        "with_ts": sum(1 for n in notes if n.get("ts_iso")),
        "detail_enriched": sum(1 for n in notes if n.get("detail_enriched")),
    }

    # ---- 情感 × 互动交叉 ----
    sentiment_stats = {}
    for s in sorted(set(n.get("sentiment") or "neutral" for n in notes)):
        group = [n for n in notes if (n.get("sentiment") or "neutral") == s]
        er = [float(n.get("engagement_rate") or 0.0) for n in group]
        ht = [float(n.get("heat_score") or 0.0) for n in group]
        sentiment_stats[s] = {
            "count": len(group),
            "mean_engagement": round(statistics.fmean(er) if er else 0.0, 4),
            "mean_heat": round(statistics.fmean(ht) if ht else 0.0, 4),
        }

    # ---- 话题 × 互动交叉 ----
    topic_stats = {}
    for n in notes:
        for t in (n.get("topics") or []):
            row = topic_stats.setdefault(t, {"freq": 0, "_er": 0.0, "_heat": 0.0})
            row["freq"] += 1
            row["_er"] += float(n.get("engagement_rate") or 0.0)
            row["_heat"] += float(n.get("heat_score") or 0.0)
    top_topics_ranked = [
        {
            "topic": t,
            "freq": row["freq"],
            "mean_engagement": round(row["_er"] / row["freq"], 4),
            "mean_heat": round(row["_heat"] / row["freq"], 4),
        }
        for t, row in topic_stats.items()
    ]
    top_topics_ranked.sort(key=lambda x: -x["freq"])
    top_topics_ranked = top_topics_ranked[:20]

    # ---- 分布直方图 ----
    def _histogram(values, buckets):
        out = []
        n_all = len(values)
        for lo, hi in buckets:
            if hi is None:
                n = sum(1 for v in values if v >= lo)
                label = f"{lo}+"
            else:
                n = sum(1 for v in values if lo <= v < hi)
                label = f"{lo}-{hi}"
            out.append({"range": label, "count": n,
                        "pct": round(n / n_all * 100, 1) if n_all else 0.0})
        return out

    er_hist = _histogram(er_values, [(0, 0.1), (0.1, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 10), (10, None)])
    heat_hist = _histogram(heat_values, [(0, 2), (2, 4), (4, 5), (5, 6), (6, 7), (7, None)])

    keyword_counter = Counter()
    topic_counter = Counter()
    for n in notes:
        for kw in (n.get("keywords") or []):
            keyword_counter[kw] += 1
        for t in (n.get("topics") or []):
            topic_counter[t] += 1

    by_day = Counter()
    for n in notes:
        ts_iso = n.get("ts_iso") or ""
        if ts_iso:
            day = ts_iso[:10]
            if day:
                by_day[day] += 1

    top_notes = sorted(notes, key=lambda n: float(n.get("heat_score") or 0.0), reverse=True)[:10]
    user_rows = sorted(
        users.values(),
        key=lambda u: u["total_liked"] * 1 + u["total_collected"] * 3 + u["total_comment"] * 2,
        reverse=True,
    )[:10]
    flagged = []
    for n in notes:
        ad = float(n.get("ad_like_score") or 0)
        if ad >= 0.5:
            flagged.append((n, "ad_like_high"))
        elif n.get("is_short") and (n.get("interact") or {}).get("liked", 0) >= 1000:
            flagged.append((n, "short_but_viral"))
    flagged = flagged[:20]

    # ---- 评论维度 ----
    comment_sentiment_counter = Counter(c.get("sentiment") or "neutral" for c in comments)
    comment_keyword_counter = Counter()
    top_commenters = {}  # user_id -> {count, total_liked, nickname}
    top_comments = sorted(
        [c for c in comments if c.get("endpoint") == "comment/page"],  # 顶层评论
        key=lambda c: c.get("liked", 0),
        reverse=True,
    )[:10]
    sub_comment_count = sum(1 for c in comments if c.get("endpoint") == "comment/page/sub")
    for c in comments:
        for kw in (c.get("keywords") or []):
            comment_keyword_counter[kw] += 1
        u = (c.get("user") or {})
        uid = u.get("user_id")
        if not uid:
            continue
        # 把评论用户也加入 users (可能笔记用户里没有)
        users.setdefault(uid, {
            "user_id": uid,
            "nickname": u.get("nickname") or "",
            "notes": 0,
            "comments": 0,
            "total_liked": 0,
            "total_collected": 0,
            "total_comment": 0,
            "fans": 0,
        })
        users[uid]["comments"] += 1
        users[uid]["total_liked"] += int(c.get("liked") or 0)
        t = top_commenters.setdefault(uid, {
            "user_id": uid,
            "nickname": u.get("nickname") or "",
            "comments": 0,
            "total_liked": 0,
        })
        t["comments"] += 1
        t["total_liked"] += int(c.get("liked") or 0)
        if not t["nickname"]:
            t["nickname"] = u.get("nickname") or ""
    top_commenter_rows = sorted(
        top_commenters.values(),
        key=lambda u: u["total_liked"] * 1 + u["comments"] * 2,
        reverse=True,
    )[:10]
    # 重新按互动量排序 user_rows (笔记 + 评论合并)
    user_rows = sorted(
        users.values(),
        key=lambda u: u["total_liked"] + u["total_collected"] * 3 + u["total_comment"] * 2 + u["comments"],
        reverse=True,
    )[:10]

    liked_values = [int(c.get("liked") or 0) for c in comments]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(records),
        "notes": len(notes),
        "comments": len(comments),
        "comment_top_level": sum(1 for c in comments if c.get("endpoint") == "comment/page"),
        "comment_sub": sub_comment_count,
        "hotlist_items": len(hotlist),
        "user_search_items": len(users_search),
        "users": len(users),

        "by_type": dict(type_counter),
        "by_sentiment": dict(sentiment_counter),
        "coverage": coverage,
        "sentiment_stats": sentiment_stats,
        "top_topics_ranked": top_topics_ranked,
        "er_hist": er_hist,
        "heat_hist": heat_hist,
        "engagement": {
            "mean": round(statistics.fmean(er_values) if er_values else 0.0, 6),
            "p50": round(_percentile(er_values, 0.5), 6),
            "p90": round(_percentile(er_values, 0.9), 6),
            "max": round(max(er_values) if er_values else 0.0, 6),
        },
        "heat": {
            "mean": round(statistics.fmean(heat_values) if heat_values else 0.0, 4),
            "p50": round(_percentile(heat_values, 0.5), 4),
            "p90": round(_percentile(heat_values, 0.9), 4),
            "max": round(max(heat_values) if heat_values else 0.0, 4),
        },
        "by_day": dict(sorted(by_day.items())),
        "top_keywords": [{"word": w, "freq": c} for w, c in keyword_counter.most_common(20)],
        "top_topics": [{"topic": w, "freq": c} for w, c in topic_counter.most_common(20)],
        "top_notes": [
            {
                "note_id": n.get("note_id"),
                "title": n.get("title"),
                "user": (n.get("user") or {}).get("nickname"),
                "liked": (n.get("interact") or {}).get("liked"),
                "heat_score": n.get("heat_score"),
                "summary": n.get("summary"),
                "topics": n.get("topics") or [],
                "sentiment": n.get("sentiment"),
                "share_url": n.get("share_url"),
                "detail_enriched": n.get("detail_enriched", False),
            }
            for n in top_notes
        ],
        "top_users": [
            {
                "user_id": u["user_id"],
                "nickname": u["nickname"],
                "notes": u["notes"],
                "comments": u["comments"],
                "total_liked": u["total_liked"],
                "total_collected": u["total_collected"],
                "total_comment": u["total_comment"],
                "fans": u["fans"],
            }
            for u in user_rows
        ],
        "flagged": [
            {
                "reason": r,
                "note_id": n.get("note_id"),
                "title": n.get("title"),
                "ad_like_score": n.get("ad_like_score"),
                "liked": (n.get("interact") or {}).get("liked"),
                "short": n.get("is_short"),
            }
            for n, r in flagged
        ],

        # ---- 评论专属 ----
        "comment_by_sentiment": dict(comment_sentiment_counter),
        "comment_top_keywords": [{"word": w, "freq": c} for w, c in comment_keyword_counter.most_common(20)],
        "comment_liked_stats": {
            "mean": round(statistics.fmean(liked_values) if liked_values else 0.0, 2),
            "p50": round(_percentile(liked_values, 0.5), 2),
            "p90": round(_percentile(liked_values, 0.9), 2),
            "max": max(liked_values) if liked_values else 0,
        },
        "top_comments": [
            {
                "comment_id": c.get("comment_id"),
                "parent_comment_id": c.get("parent_comment_id"),
                "is_sub": c.get("is_sub_comment", False),
                "user": (c.get("user") or {}).get("nickname"),
                "content": (c.get("content") or "")[:200],
                "liked": c.get("liked"),
                "sentiment": c.get("sentiment"),
                "keywords": c.get("keywords") or [],
                "ts_iso": c.get("ts_iso"),
                "ip_location": c.get("ip_location"),
            }
            for c in top_comments
        ],
        "top_commenters": [
            {
                "user_id": u["user_id"],
                "nickname": u["nickname"],
                "comments": u["comments"],
                "total_liked": u["total_liked"],
            }
            for u in top_commenter_rows
        ],

        # ---- 热门榜 / 用户搜索专属 ----
        "top_hotlist": [
            {"word_id": h.get("word_id"), "query": h.get("query"), "score": h.get("score")}
            for h in sorted(hotlist, key=lambda h: float(h.get("score") or 0), reverse=True)[:20]
        ],
    }
    return summary

def export_notes_csv(records, path):
    """导出笔记明细 CSV (UTF-8 BOM, Excel 可直接打开)。返回行数。"""
    import csv
    notes = [r for r in records if not r.get("is_comment") and not r.get("is_hotlist") and not r.get("is_user")]
    fields = ["note_id", "title", "desc_plain", "tags", "type", "detail_enriched",
              "user_id", "nickname", "fans",
              "liked", "collected", "comment", "share",
              "engagement_rate", "heat_score", "sentiment", "sentiment_score",
              "keywords", "ts_iso", "ip_location", "share_url"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for n in notes:
            w.writerow({
                "note_id": n.get("note_id"),
                "title": n.get("title"),
                "desc_plain": n.get("desc_plain") or "",
                "tags": "|".join(n.get("tags") or []),
                "type": n.get("type"),
                "detail_enriched": n.get("detail_enriched", False),
                "user_id": (n.get("user") or {}).get("user_id"),
                "nickname": (n.get("user") or {}).get("nickname"),
                "fans": (n.get("user") or {}).get("fans"),
                "liked": (n.get("interact") or {}).get("liked"),
                "collected": (n.get("interact") or {}).get("collected"),
                "comment": (n.get("interact") or {}).get("comment"),
                "share": (n.get("interact") or {}).get("share"),
                "engagement_rate": n.get("engagement_rate"),
                "heat_score": n.get("heat_score"),
                "sentiment": n.get("sentiment"),
                "sentiment_score": n.get("sentiment_score"),
                "keywords": "|".join(n.get("keywords") or []),
                "ts_iso": n.get("ts_iso"),
                "ip_location": n.get("ip_location"),
                "share_url": n.get("share_url"),
            })
    return len(notes)


def export_comments_csv(records, path):
    """导出评论明细 CSV (UTF-8 BOM)。返回行数。"""
    import csv
    comments = [r for r in records if r.get("is_comment")]
    fields = ["comment_id", "note_id", "is_sub_comment", "parent_comment_id",
              "user_id", "nickname", "content", "liked", "sentiment", "sentiment_score",
              "keywords", "ts_iso", "ip_location"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for c in comments:
            w.writerow({
                "comment_id": c.get("comment_id"),
                "note_id": c.get("note_id"),
                "is_sub_comment": c.get("is_sub_comment", False),
                "parent_comment_id": c.get("parent_comment_id"),
                "user_id": (c.get("user") or {}).get("user_id"),
                "nickname": (c.get("user") or {}).get("nickname"),
                "content": c.get("content"),
                "liked": c.get("liked"),
                "sentiment": c.get("sentiment"),
                "sentiment_score": c.get("sentiment_score"),
                "keywords": "|".join(c.get("keywords") or []),
                "ts_iso": c.get("ts_iso"),
                "ip_location": c.get("ip_location"),
            })
    return len(comments)


def render_markdown(summary, source):
    """渲染洞察式 Markdown 报告。

    章节:
      1. 核心结论 (自动生成要点)
      2. 样本与数据质量
      3. 互动与热度分布 (含直方图)
      4. 关键词 Top 20
      5. 话题 Top 20 (含互动交叉)
      6. 情感 × 互动交叉
      7. 时间分布
      8. Top 10 笔记 (按热度)
      9. Top 10 用户
      10. 评论分析 (若有)
      11. 风险 / 异常样本
      12. 方法论与限制
    """
    lines = []
    lines.append("# 小红书数据汇总分析报告")
    lines.append("")
    lines.append(f"- 数据源: `{source}`")
    lines.append(f"- 生成时间: {summary['generated_at']}")
    lines.append(
        f"- 样本: {summary['total']} 条记录 (笔记 {summary['notes']}, 评论 {summary['comments']} "
        f"[顶层 {summary['comment_top_level']} + 子 {summary['comment_sub']}], "
        f"热门榜 {summary['hotlist_items']}, 用户搜索 {summary['user_search_items']})"
    )
    lines.append("")

    total = summary["notes"]
    cov = summary.get("coverage") or {}

    # ---- 1. 核心结论 ----
    insights = []
    if total:
        base = f"共 {total} 条相关笔记、{summary['users']} 位作者"
        if summary["comments"]:
            base += f"、{summary['comments']} 条评论"
        insights.append(base + "。")
        if summary["top_keywords"]:
            kw = summary["top_keywords"][0]
            share = f" (占笔记 {kw['freq'] / total * 100:.0f}%)" if total else ""
            insights.append(f"主题集中度: 最高频关键词「{kw['word']}」出现 {kw['freq']} 次{share}。")
        bs = summary["by_sentiment"]
        if bs:
            neg = bs.get("negative", 0)
            pos = bs.get("positive", 0)
            neutral = bs.get("neutral", 0)
            neg_part = f"负面占比 {neg / total * 100:.1f}%" if total and neg else "无负面样本"
            insights.append(f"情感构成: 正面 {pos} / 中性 {neutral} / 负面 {neg} ({neg_part})。")
        if summary["top_notes"]:
            tn = summary["top_notes"][0]
            insights.append(f"头部内容: 「{tn['title']}」赞 {tn['liked']}, 热度分 {tn['heat_score']}。")
        note_authors = [u for u in summary["top_users"] if u["notes"] > 0]
        if note_authors:
            tu = note_authors[0]
            insights.append(
                f"高互动作者: {tu['nickname']} ({tu['notes']} 篇笔记, "
                f"互动总量 赞{tu['total_liked']} + 藏{tu['total_collected']} + 评{tu['total_comment']})。"
            )
        overall_er = summary["engagement"]["mean"] or 0.0
        ss = summary.get("sentiment_stats") or {}
        for key, label in (("positive", "正面"), ("negative", "负面")):
            st = ss.get(key)
            if st and st["count"] >= 3 and overall_er > 0:
                ratio = st["mean_engagement"] / overall_er
                if ratio >= 1.5:
                    insights.append(
                        f"{label}内容平均互动率是整体的 {ratio:.1f} 倍 "
                        f"({st['mean_engagement']:.2f} vs {overall_er:.2f}), 内容特征值得拆解。"
                    )
                elif ratio <= 0.4:
                    insights.append(
                        f"{label}内容平均互动率仅为整体的 {ratio:.1f} 倍 "
                        f"({st['mean_engagement']:.2f} vs {overall_er:.2f})。"
                    )
        gap = cov.get("notes", 0) - cov.get("with_desc", 0)
        if gap > 0:
            insights.append(
                f"数据缺口: {gap} 条搜索卡片仅有标题 (v2 搜索接口不返回正文/时间戳), "
                f"正文/话题/时间维度分析基于已补全的 {cov.get('with_desc', 0)} 条笔记。"
            )
    lines.append("## 1. 核心结论")
    lines.append("")
    if insights:
        for i in insights:
            lines.append(f"- {i}")
    else:
        lines.append("- 本次数据集中没有有效笔记, 无法生成结论。")
    lines.append("")

    # ---- 2. 样本与数据质量 ----
    lines.append("## 2. 样本与数据质量")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 笔记总数 | {cov.get('notes', 0)} |")
    lines.append(f"| 去重用户数 | {summary['users']} |")
    lines.append(f"| 含正文 (desc) | {cov.get('with_desc', 0)} |")
    lines.append(f"| 已补全详情 | {cov.get('detail_enriched', 0)} |")
    lines.append(f"| 含时间戳 | {cov.get('with_ts', 0)} |")
    if summary["comments"]:
        lines.append(f"| 评论 | {summary['comments']} (顶层 {summary['comment_top_level']} + 子 {summary['comment_sub']}) |")
    lines.append("")

    # ---- 3. 互动与热度分布 ----
    lines.append("## 3. 互动与热度分布")
    lines.append("")
    lines.append("> 互动率 = (赞 + 收藏 + 评论) / max(粉丝数, 1000); 热度分 = log1p(赞 + 3×收藏 + 2×评论 + 4×分享) × 时效因子。")
    lines.append("")
    eng = summary["engagement"]
    heat = summary["heat"]
    lines.append("| 指标 | 均值 | P50 | P90 | 最大 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(f"| 互动率 | {eng['mean']:.4f} | {eng['p50']:.4f} | {eng['p90']:.4f} | {eng['max']:.4f} |")
    lines.append(f"| 热度分 | {heat['mean']:.2f} | {heat['p50']:.2f} | {heat['p90']:.2f} | {heat['max']:.2f} |")
    lines.append("")

    def _bar(c, mx):
        if c <= 0 or mx <= 0:
            return ""
        return "█" * max(1, round(24 * c / mx))

    if summary.get("er_hist"):
        mx = max((b["count"] for b in summary["er_hist"]), default=0)
        lines.append("**互动率分布**")
        lines.append("")
        lines.append("| 区间 | 笔记数 | 占比 |")
        lines.append("| --- | --- | --- |")
        for b in summary["er_hist"]:
            lines.append(f"| {b['range']} | {b['count']} ({b['pct']}%) | {_bar(b['count'], mx)} |")
        lines.append("")
    if summary.get("heat_hist"):
        mx = max((b["count"] for b in summary["heat_hist"]), default=0)
        lines.append("**热度分分布**")
        lines.append("")
        lines.append("| 区间 | 笔记数 | 占比 |")
        lines.append("| --- | --- | --- |")
        for b in summary["heat_hist"]:
            lines.append(f"| {b['range']} | {b['count']} ({b['pct']}%) | {_bar(b['count'], mx)} |")
        lines.append("")
    lines.append("**笔记类型 / 情感倾向**")
    lines.append("")
    for k, v in (summary["by_type"] or {}).items():
        lines.append(f"- 类型 {k}: {v}")
    for k, v in (summary["by_sentiment"] or {}).items():
        lines.append(f"- 情感 {k}: {v}")
    lines.append("")

    # ---- 4. 关键词 ----
    if summary["top_keywords"]:
        lines.append("## 4. 关键词 Top 20")
        lines.append("")
        lines.append("| 排名 | 关键词 | 频次 |")
        lines.append("| --- | --- | --- |")
        for i, item in enumerate(summary["top_keywords"], 1):
            lines.append(f"| {i} | {item['word']} | {item['freq']} |")
        lines.append("")

    # ---- 5. 话题 (含互动交叉) ----
    if summary.get("top_topics_ranked"):
        lines.append("## 5. 话题 Top 20 (含互动交叉)")
        lines.append("")
        lines.append("| 排名 | 话题 | 频次 | 平均互动率 | 平均热度 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for i, t in enumerate(summary["top_topics_ranked"], 1):
            lines.append(
                f"| {i} | {t['topic']} | {t['freq']} | {t['mean_engagement']:.4f} | {t['mean_heat']:.2f} |"
            )
        lines.append("")

    # ---- 6. 情感 × 互动 ----
    if summary.get("sentiment_stats"):
        lines.append("## 6. 情感 × 互动交叉")
        lines.append("")
        lines.append("| 情感 | 笔记数 | 占比 | 平均互动率 | 平均热度 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s, st in summary["sentiment_stats"].items():
            pct = f"{st['count'] / total * 100:.1f}%" if total else "-"
            lines.append(f"| {s} | {st['count']} | {pct} | {st['mean_engagement']:.4f} | {st['mean_heat']:.2f} |")
        lines.append("")

    # ---- 7. 时间分布 ----
    if summary["by_day"]:
        lines.append("## 7. 时间分布 (按天)")
        lines.append("")
        for day, c in (summary["by_day"] or {}).items():
            lines.append(f"- {day}: {c}")
        lines.append("")

    # ---- 8. Top 笔记 ----
    if summary["top_notes"]:
        lines.append("## 8. Top 10 笔记 (按热度)")
        lines.append("")
        for i, n in enumerate(summary["top_notes"], 1):
            title = n.get("title") or "(无标题)"
            user = n.get("user") or "?"
            mark = " ✅已补全正文" if n.get("detail_enriched") else ""
            lines.append(
                f"{i}. **{title}**{mark} — @{user} · 赞 {n.get('liked')} · heat {n.get('heat_score')}"
            )
            if n.get("summary"):
                lines.append(f"   > {n['summary']}")
            if n.get("topics"):
                lines.append(f"   话题: {', '.join(n['topics'])} · 情感 {n.get('sentiment')}")
            if n.get("share_url"):
                lines.append(f"   链接: {n['share_url']}")
        lines.append("")

    # ---- 9. Top 用户 ----
    if summary["top_users"]:
        lines.append("## 9. Top 10 用户 (按互动总量)")
        lines.append("")
        lines.append("| 用户 | 笔记 | 评论 | 赞 | 收藏 | 粉丝 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for u in summary["top_users"]:
            lines.append(
                f"| {u['nickname']} ({u['user_id']}) | {u['notes']} | {u['comments']} | "
                f"{u['total_liked']} | {u['total_collected']} | {u['fans']} |"
            )
        lines.append("")

    # ---- 10. 评论分析 ----
    if summary["comments"] > 0:
        lines.append("## 10. 评论分析")
        lines.append("")
        cls = summary["comment_liked_stats"]
        lines.append(
            f"- 评论总数 {summary['comments']} (顶层 {summary['comment_top_level']}, "
            f"子评论 {summary['comment_sub']})"
        )
        lines.append(f"- 点赞数: 均值 {cls['mean']:.2f}, 中位 {cls['p50']:.2f}, P90 {cls['p90']:.2f}, 最大 {cls['max']}")
        lines.append("")
        if summary["comment_by_sentiment"]:
            lines.append("**评论情感**")
            lines.append("")
            for k, v in summary["comment_by_sentiment"].items():
                lines.append(f"- {k}: {v}")
            lines.append("")
        if summary["comment_top_keywords"]:
            lines.append("**评论 Top 15 关键词**")
            lines.append("")
            lines.append("| 排名 | 关键词 | 频次 |")
            lines.append("| --- | --- | --- |")
            for i, item in enumerate(summary["comment_top_keywords"][:15], 1):
                lines.append(f"| {i} | {item['word']} | {item['freq']} |")
            lines.append("")
        if summary["top_comments"]:
            lines.append("**评论 Top 10 高赞**")
            lines.append("")
            for i, c in enumerate(summary["top_comments"], 1):
                user = c.get("user") or "?"
                kind = "(子)" if c.get("is_sub") else ""
                sentiment = c.get("sentiment") or "?"
                kws = ", ".join(c.get("keywords") or [])[:80]
                content = (c.get("content") or "").replace("\n", " ")
                lines.append(
                    f"{i}. [{c.get('liked', 0)}赞] [{sentiment}] @{user} {kind}: {content[:200]}"
                )
                if kws:
                    lines.append(f"   关键词: {kws}")
            lines.append("")
        if summary["top_commenters"]:
            lines.append("**Top 10 评论者**")
            lines.append("")
            lines.append("| 用户 | 评论数 | 总赞 |")
            lines.append("| --- | --- | --- |")
            for u in summary["top_commenters"]:
                lines.append(f"| {u['nickname']} ({u['user_id']}) | {u['comments']} | {u['total_liked']} |")
            lines.append("")

    # ---- 11. 风险 ----
    lines.append("## 11. 风险 / 异常样本 (笔记维度)")
    lines.append("")
    if not summary["flagged"]:
        lines.append("本次数据集中未命中任何广告或异常模式。")
    else:
        lines.append("| 原因 | 标题 | 赞 | ad_like | 短笔记 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in summary["flagged"]:
            lines.append(f"| {f['reason']} | {f['title']} | {f['liked']} | {f['ad_like_score']} | {f['short']} |")
    lines.append("")

    # ---- 热门榜 ----
    if summary["top_hotlist"]:
        lines.append("## 11.1 热门榜 Top 20")
        lines.append("")
        lines.append("| 排名 | 词条 | 热度分 |")
        lines.append("| --- | --- | --- |")
        for i, h in enumerate(summary["top_hotlist"], 1):
            lines.append(f"| {i} | {h['query']} | {h['score']} |")
        lines.append("")

    # ---- 12. 方法论与限制 ----
    lines.append("## 12. 方法论与限制")
    lines.append("")
    lines.append("- 采集: 浏览器页面驱动 (v2 关键词搜索 + 笔记详情页 INITIAL_STATE + 评论 API), 基于用户登录态 cookie, 每次请求间有随机限速。")
    lines.append("- 互动率 = (赞 + 收藏 + 评论) / max(粉丝数, 1000); 热度分 = log1p(赞 + 3×收藏 + 2×评论 + 4×分享) × 时效因子 (7 天半衰)。")
    lines.append("- 情感: 内置中英情感词典启发式 (非大模型), 关键词: jieba TF-IDF。")
    lines.append("- 限制: ① 搜索卡片不含正文/时间戳, 正文/话题/时间维度仅覆盖已补全笔记; ② 样本为关键词搜索结果, 非随机抽样; ③ 指标为相对量, 跨关键词横向对比时注意口径一致。")

    lines.append("---")
    lines.append("")
    lines.append("> 本报告由 xiaohongshu-harvester skill 生成 (analyze.py)。")
    lines.append("> 数据为本地缓存, 仅供参考; 不得用于绕过小红书平台规则或商业再分发。")
    return "\n".join(lines)

def main(argv=None):
    p = argparse.ArgumentParser(description="小红书汇总分析与报告")
    p.add_argument("--in", dest="src", required=True, help="输入 enriched JSONL")
    p.add_argument("--report", required=True, help="输出 Markdown 报告路径")
    p.add_argument("--summary", required=True, help="输出 summary JSON 路径")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    records = _read_jsonl(Path(args.src))
    LOG.info("读入 %d 条记录", len(records))

    summary = aggregate(records)
    _write_json(Path(args.summary), summary)
    LOG.info("已写入 %s", args.summary)

    md = render_markdown(summary, args.src)
    _write_text(Path(args.report), md)
    LOG.info("已写入 %s", args.report)
    print(f"summary={args.summary}, report={args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
