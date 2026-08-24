# -*- coding: utf-8 -*-
"""
cross_analyze.py - 跨 run 聚合分析

agent 完成多关键词 pipeline 跑完后, 调用此脚本把多个 run 的 enriched.jsonl
按调研维度合并, 输出结构化 JSON 给 agent 写报告时直接读。

不做语义聚类, 只做关键词命中 + 正文字面匹配; 输出 JSON 后由 agent
基于用户关心的具体问题, 标注 note_id / 评论原文生成针对性总结。

CLI:
  python scripts/cross_analyze.py --runs <slug1>,<slug2>... \\
      --dimensions hc,interview,company \\
      --output data/runs/_cross_analyze.json

  slug 是 run folder 名字后缀 (pipeline --topic 传入), 不是日期前缀。
  维度名: hc / interview / company / lifestyle / safety / consumer
  (按需扩展, 默认只跑 hc / interview / company 三档)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

LOG_NAME = "cross_analyze"

# 维度关键词集. 粗糙够用; agent 后续可基于 JSON 二次过滤.
DIM_KEYWORDS: Dict[str, Set[str]] = {
    "hc": {
        # HC 缩减 / 面试机会
        "缩招", "HC 缩减", "HC 砍", "没 HC", "hc 缩减", "hc 砍", "没 hc",
        "hc 不够", "hc 充足", "hc 还行", "hc 多", "扩招", "逆势扩招", "回暖",
        "招聘回暖", "hc 缩水", "砍 hc", "HC 缩", "招满",
        # 秋招节奏
        "提前批", "秋招", "秋招提前批", "校招", "暑期实习", "暑期转正",
        "池子", "泡池子", "排序", "活水",
    },
    "interview": {
        # 面试经验 / 准备策略
        "面经", "面试", "一面", "二面", "三面", "HR 面", "群面", "技术面",
        "笔试", "算法题", "leetcode", "代码题", "系统设计", "反问",
        "自我介绍", "面试技巧", "面试经验", "面试准备", "实习面试",
        "面试官", "通过", "拿到 offer", "上岸", "挂了", "挂了挂了",
    },
    "company": {
        # 公司 / 行业口碑
        "大厂", "中厂", "字节", "阿里", "腾讯", "美团", "京东", "百度",
        "拼多多", "快手", "小红书", "华为", "华子", "小米", "滴滴",
        "b 站", "bilibili", "微软", "外企", "国企", "央企", "银行",
        "事业编", "考公",
        # 工作强度 / 待遇
        "wlb", "955", "1075", "996", "007", "加班", "内卷", "卷",
        "35 岁", "中年危机", "裁员", "被裁", "被优化", "降薪",
        "工作氛围", "leader", "技术栈", "晋升", "职级",
        # 负面口碑
        "避雷", "吐槽", "毁约", "压价", "低薪",
    },
    "lifestyle": {
        # 露营 / 旅游 / 户外 / 美食 等生活类话题
        "露营", "帐篷", "天幕", "营地", "烧烤", "野餐", "徒步",
        "装备", "新手", "出行", "周边游", "户外",
    },
    "consumer": {
        # 消费品 / 数码 / 护肤 / 家居 等
        "护肤", "彩妆", "口红", "粉底", "面膜", "精华",
        "手机", "笔记本", "耳机", "相机", "家电",
        "回购", "种草", "避雷", "测评", "好物",
    },
    "safety": {
        # 安全 / 隐患 / 翻车
        "危险", "隐患", "翻车", "出事故", "事故", "翻车现场",
        "漏电", "起火", "爆炸", "烫伤", "受伤", "中毒", "卫生问题",
    },
}


def _has_any(text: str, words: Set[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(w.lower() in t for w in words)


def _load_run(slug: str, workspace: Path) -> List[dict]:
    """加载单个 run folder 的 enriched.jsonl. 支持同 slug 的多个副本 (取最新的)."""
    today = None
    candidates = []
    for p in workspace.iterdir():
        if not p.is_dir():
            continue
        # 形如 2026-08-24_<slug> 或 2026-08-24_<slug>_1 / _2
        parts = p.name.split("_", 1)
        if len(parts) != 2:
            continue
        date_part, name_part = parts
        # 去掉 _N 后缀
        name_part = re.sub(r"_\d+$", "", name_part)
        if name_part != slug:
            continue
        today = date_part  # 用最新一次扫到的日期
        candidates.append((p, name_part))

    if not candidates:
        return []
    # 取最后一个 (按字典序, _2 > _1 > base)
    candidates.sort(key=lambda x: x[0].name)
    run_dir, _ = candidates[-1]
    f = run_dir / "enriched.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _notes_by_dim(records: List[dict], dim_words: Set[str]) -> List[dict]:
    out = []
    for r in records:
        if r.get("is_comment"):
            continue
        full_text = (r.get("title") or "") + "\n" + (r.get("desc_plain") or "")
        if _has_any(full_text, dim_words):
            out.append({
                "note_id": r.get("note_id"),
                "title": r.get("title") or "",
                "desc_plain": r.get("desc_plain") or "",
                "tags": r.get("tags") or [],
                "liked": (r.get("interact") or {}).get("liked"),
                "comment_count": (r.get("interact") or {}).get("comment"),
                "detail_enriched": r.get("detail_enriched", False),
                "user": (r.get("user") or {}).get("nickname"),
                "ts_iso": r.get("ts_iso"),
                "share_url": r.get("share_url") or "",
            })
    return out


def _comments_by_dim(records: List[dict], dim_words: Set[str]) -> List[dict]:
    out = []
    for r in records:
        if not r.get("is_comment"):
            continue
        text = r.get("content") or ""
        if _has_any(text, dim_words):
            out.append({
                "note_id": r.get("note_id"),
                "comment_id": r.get("comment_id"),
                "content": text,
                "liked": r.get("liked"),
                "user": (r.get("user") or {}).get("nickname"),
                "is_sub": r.get("is_sub_comment", False),
                "ts_iso": r.get("ts_iso"),
                "ip_location": r.get("ip_location"),
            })
    return out


def aggregate(runs: List[str], dimensions: List[str], workspace: Path) -> dict:
    out: dict = {
        "by_dimension": {},
        "totals": {
            "runs": runs,
            "notes_total": 0,
            "comments_total": 0,
            "dimensions": dimensions,
        },
    }
    for dim in dimensions:
        words = DIM_KEYWORDS.get(dim)
        if not words:
            print(f"WARN: unknown dimension '{dim}', skip", file=sys.stderr)
            continue
        dim_notes: List[dict] = []
        dim_comments: List[dict] = []
        per_run: Dict[str, dict] = {}
        for slug in runs:
            records = _load_run(slug, workspace)
            per_run[slug] = {
                "records_total": len(records),
                "notes_matched": sum(1 for r in _notes_by_dim(records, words)),
                "comments_matched": sum(1 for r in _comments_by_dim(records, words)),
            }
            for n in _notes_by_dim(records, words):
                n["run"] = slug
                dim_notes.append(n)
            for c in _comments_by_dim(records, words):
                c["run"] = slug
                dim_comments.append(c)
        # 排序取 Top
        top_notes = sorted(dim_notes, key=lambda x: (x["liked"] or 0), reverse=True)[:10]
        top_comments = sorted(
            [c for c in dim_comments if not c["is_sub"]],
            key=lambda x: (x["liked"] or 0), reverse=True,
        )[:10]
        full_notes = sorted(
            [n for n in dim_notes if n["desc_plain"]],
            key=lambda x: (x["liked"] or 0), reverse=True,
        )[:5]
        out["by_dimension"][dim] = {
            "notes_count": len(dim_notes),
            "comments_count": len(dim_comments),
            "per_run": per_run,
            "top_notes_by_liked": [
                {
                    "note_id": n["note_id"], "run": n["run"], "title": n["title"],
                    "liked": n["liked"], "comment_count": n["comment_count"],
                    "has_body": bool(n["desc_plain"]),
                }
                for n in top_notes
            ],
            "top_comments_by_liked": [
                {
                    "comment_id": c["comment_id"], "run": c["run"],
                    "note_id": c["note_id"], "content": c["content"],
                    "liked": c["liked"], "user": c["user"],
                }
                for c in top_comments
            ],
            "full_notes": [
                {
                    "note_id": n["note_id"], "run": n["run"], "title": n["title"],
                    "desc_plain": n["desc_plain"], "tags": n["tags"],
                    "liked": n["liked"], "comment_count": n["comment_count"],
                    "user": n["user"], "ts_iso": n["ts_iso"],
                    "share_url": n["share_url"],
                }
                for n in full_notes
            ],
        }
        out["totals"]["notes_total"] += len(dim_notes)
        out["totals"]["comments_total"] += len(dim_comments)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="跨 run 聚合分析 (按调研维度合并多 run enriched.jsonl)")
    p.add_argument("--runs", required=True,
                   help="逗号分隔的 run slug, 多个关键词的 --topic 值")
    p.add_argument("--dimensions", default="hc,interview,company",
                   help="逗号分隔的调研维度, 默认 hc / interview / company")
    p.add_argument("--workspace", default="data/runs",
                   help="workspace 根目录 (默认 data/runs)")
    p.add_argument("--output", default=None,
                   help="输出 JSON 路径, 默认 <workspace>/_cross_analyze.json")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    import logging
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(LOG_NAME)

    runs = [s.strip() for s in args.runs.split(",") if s.strip()]
    dimensions = [s.strip() for s in args.dimensions.split(",") if s.strip()]
    workspace = Path(args.workspace).resolve()
    output = Path(args.output) if args.output else workspace / "_cross_analyze.json"

    log.info("聚合 %d run × %d 维度 -> %s", len(runs), len(dimensions), output)
    for r in runs:
        log.info("  run: %s", r)
    for d in dimensions:
        log.info("  dimension: %s", d)

    result = aggregate(runs, dimensions, workspace)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("写入 %s", output)

    # 摘要
    print("\n=== 摘要 ===")
    for dim, data in result["by_dimension"].items():
        print(f"  [{dim}] 笔记 {data['notes_count']} / 评论 {data['comments_count']}")
        for n in data["top_notes_by_liked"][:3]:
            mark = "[正文]" if n["has_body"] else "[标题]"
            print(f"    {mark} [{n['run']}/{n['note_id'][:8]}] {n['title'][:50]} (赞 {n['liked']})")
    print(f"\n  total notes: {result['totals']['notes_total']}")
    print(f"  total comments: {result['totals']['comments_total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())