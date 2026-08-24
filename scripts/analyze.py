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
    notes = [r for r in records if not r.get("is_comment")]
    comments = [r for r in records if r.get("is_comment")]
    hotlist = [r for r in records if (r.get("endpoint") or "") == "search/hotlist"]

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
        "users": len(users),

        "by_type": dict(type_counter),
        "by_sentiment": dict(sentiment_counter),
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
    }
    return summary

def render_markdown(summary, source):
    """根据 summary 渲染 Markdown 报告。

    章节:
      1. 整体概览
      2. 笔记: 类型 / 情感 / 时间分布
      3. 笔记: 关键词 / 话题 / Top
      4. 评论: 情感 / 关键词 / 赞分布
      5. 评论: Top 10 高赞 + Top 评论者
      6. 风险 / 异常样本
    """
    lines = []
    lines.append("# 小红书数据汇总分析报告")
    lines.append("")
    lines.append(f"- 数据源: `{source}`")
    lines.append(f"- 生成时间: {summary['generated_at']}")
    lines.append(
        f"- 总记录数: {summary['total']} "
        f"(笔记 {summary['notes']}, 评论 {summary['comments']} "
        f"[顶层 {summary['comment_top_level']} + 子 {summary['comment_sub']}], "
        f"热门榜 {summary['hotlist_items']})"
    )
    lines.append(f"- 去重用户数: {summary['users']}")
    lines.append("")

    # ---- 笔记: 互动与热度 ----
    lines.append("## 1. 笔记 · 互动与热度")
    lines.append("")
    eng = summary["engagement"]
    heat = summary["heat"]
    lines.append("| 指标 | 均值 | P50 | P90 | 最大 |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(f"| 互动率 | {eng['mean']:.4f} | {eng['p50']:.4f} | {eng['p90']:.4f} | {eng['max']:.4f} |")
    lines.append(f"| 热度分 | {heat['mean']:.2f} | {heat['p50']:.2f} | {heat['p90']:.2f} | {heat['max']:.2f} |")
    lines.append("")
    lines.append("**笔记类型**")
    lines.append("")
    for k, v in (summary["by_type"] or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("**笔记情感倾向**")
    lines.append("")
    for k, v in (summary["by_sentiment"] or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # ---- 笔记: 时间分布 ----
    if summary["by_day"]:
        lines.append("## 2. 笔记 · 时间分布 (按天)")
        lines.append("")
        for day, c in (summary["by_day"] or {}).items():
            lines.append(f"- {day}: {c}")
        lines.append("")

    # ---- 笔记: 关键词 + 话题 ----
    if summary["top_keywords"]:
        lines.append("## 3. 笔记 · Top 20 关键词")
        lines.append("")
        lines.append("| 排名 | 关键词 | 频次 |")
        lines.append("| --- | --- | --- |")
        for i, item in enumerate(summary["top_keywords"], 1):
            lines.append(f"| {i} | {item['word']} | {item['freq']} |")
        lines.append("")
    if summary["top_topics"]:
        lines.append("## 4. 笔记 · Top 20 话题")
        lines.append("")
        lines.append("| 排名 | 话题 | 频次 |")
        lines.append("| --- | --- | --- |")
        for i, item in enumerate(summary["top_topics"], 1):
            lines.append(f"| {i} | {item['topic']} | {item['freq']} |")
        lines.append("")

    # ---- 笔记: Top 笔记 / 用户 ----
    if summary["top_notes"]:
        lines.append("## 5. 笔记 · Top 10 (按热度)")
        lines.append("")
        for i, n in enumerate(summary["top_notes"], 1):
            title = n.get("title") or "(无标题)"
            user = n.get("user") or "?"
            lines.append(f"{i}. **{title}** — @{user} · 赞 {n.get('liked')} · heat {n.get('heat_score')}")
            if n.get("summary"):
                lines.append(f"   > {n['summary']}")
            if n.get("topics"):
                lines.append(f"   话题: {', '.join(n['topics'])} · 情感 {n.get('sentiment')}")
            if n.get("share_url"):
                lines.append(f"   链接: {n['share_url']}")
        lines.append("")
    if summary["top_users"]:
        lines.append("## 6. 笔记 · Top 10 用户 (按互动总量)")
        lines.append("")
        lines.append("| 用户 | 笔记 | 评论 | 赞 | 收藏 | 粉丝 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for u in summary["top_users"]:
            lines.append(
                f"| {u['nickname']} ({u['user_id']}) | {u['notes']} | {u['comments']} | "
                f"{u['total_liked']} | {u['total_collected']} | {u['fans']} |"
            )
        lines.append("")

    # ---- 评论: 情感 / 关键词 / 赞分布 ----
    if summary["comments"] > 0:
        lines.append("## 7. 评论 · 整体概览")
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

    # ---- 评论: Top 10 高赞 ----
    if summary["top_comments"]:
        lines.append("## 8. 评论 · Top 10 高赞")
        lines.append("")
        for i, c in enumerate(summary["top_comments"], 1):
            user = c.get("user") or "?"
            kind = "(子)" if c.get("is_sub") else ""
            sentiment = c.get("sentiment") or "?"
            kws = ", ".join(c.get("keywords") or [])[:80]
            content = (c.get("content") or "").replace("\n", " ")
            lines.append(
                f"{i}. [{c.get('liked', 0)}赞] [{sentiment}] @{user} {kind}: "
                f"{content[:200]}"
            )
            if kws:
                lines.append(f"   关键词: {kws}")
        lines.append("")

    # ---- 评论: Top 评论者 ----
    if summary["top_commenters"]:
        lines.append("## 9. 评论 · Top 10 评论者")
        lines.append("")
        lines.append("| 用户 | 评论数 | 总赞 |")
        lines.append("| --- | --- | --- |")
        for u in summary["top_commenters"]:
            lines.append(f"| {u['nickname']} ({u['user_id']}) | {u['comments']} | {u['total_liked']} |")
        lines.append("")

    # ---- 风险 ----
    lines.append("## 10. 风险 / 异常样本 (笔记维度)")
    lines.append("")
    if not summary["flagged"]:
        lines.append("本次数据集中未命中任何广告或异常模式。")
    else:
        lines.append("| 原因 | 标题 | 赞 | ad_like | 短笔记 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in summary["flagged"]:
            lines.append(f"| {f['reason']} | {f['title']} | {f['liked']} | {f['ad_like_score']} | {f['short']} |")
    lines.append("")

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
