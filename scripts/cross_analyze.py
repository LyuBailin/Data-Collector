# -*- coding: utf-8 -*-
"""
cross_analyze.py - 跨 run 聚合分析 (维度由 agent 自由定义)

agent 完成多关键词 pipeline 跑完后, 调用此脚本把多个 run 的 enriched.jsonl
按 agent 定义的维度聚合, 输出结构化 JSON 给 agent 写报告时直接读。

设计原则: 维度不内置、不硬编码。agent 根据与用户讨论产出的调研方向,
自由设计维度与关键词集, 通过 --dimensions 一次性传入。脚本只做
"关键词命中 -> 按赞数排序 -> 落盘" 的纯工具, 不做语义聚类, 也不预设任何
主题相关词汇 (护肤用"烂脸"、穿搭用"版型"这类领域词由 agent 提供)。

CLI:
  python scripts/cross_analyze.py \\
      --runs pants183,daman,pangnansheng,damanpinpai \\
      --dimensions "搭配:穿搭,搭配,显瘦,显高,遮肚,版型;品牌:大码,微胖,胖男生,品牌,店铺" \\
      --workspace data/runs \\
      --output data/runs/_183chuan.json

--dimensions 格式: "维度名:关键词1,关键词2;维度名2:关键词3,关键词4"
  - 维度名: 短 ASCII 标识 (如 fit / brand), 同时作为报告章节标题
  - 关键词: 必须是该内容社区实际使用的词; 每个维度 3~8 个词为宜
  - 多个维度用分号分隔; 任何段不合法都会在参数解析期报错 (fail loud)

可选:
  --top-notes N      每个维度输出 Top N 笔记 (默认 10)
  --top-comments N   每个维度输出 Top N 评论 (默认 10)
  --full-notes N     每个维度输出带完整正文的笔记数 (默认 5)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set

# 强制 stdout/stderr 用 UTF-8, 避免 Windows PowerShell (GBK) 环境下
# 标题含 emoji / CJK 扩展字符时 UnicodeEncodeError 崩溃.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
elif sys.platform == "win32":
    import io
    if isinstance(sys.stdout, io.TextIOBase):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import logging  # noqa: E402

LOG_NAME = "cross_analyze"
LOG = logging.getLogger(LOG_NAME)


# ----------------------------- 维度解析 -----------------------------


def parse_dimensions(raw: str) -> Dict[str, Set[str]]:
    """解析 'name:kw1,kw2;name2:kw3,kw4' -> {name: {kw...}}.

    任何段不合法 (缺冒号 / 空维度名 / 空关键词 / 维度名重复) 都直接抛错,
    由调用方在参数解析期 fail loud, 不静默跳过。
    """
    out: Dict[str, Set[str]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"维度段 '{chunk}' 缺冒号, 应为 '维度名:关键词1,关键词2'")
        name, kws = chunk.split(":", 1)
        name = name.strip()
        kw_set = {k.strip() for k in kws.split(",") if k.strip()}
        if not name:
            raise ValueError(f"维度段 '{chunk}' 维度名为空")
        if not kw_set:
            raise ValueError(f"维度段 '{chunk}' 关键词为空")
        if name in out:
            raise ValueError(f"维度名重复: '{name}'")
        out[name] = kw_set
    if not out:
        raise ValueError("--dimensions 为空, 至少需要一个 '维度名:关键词' 段")
    return out


# ----------------------------- 命中与加载 -----------------------------


def _has_any(text: str, words: Set[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(w.lower() in t for w in words)


def _load_run(slug: str, workspace: Path) -> List[dict]:
    """加载单个 run folder 的 enriched.jsonl. 支持同 slug 的多个副本 (取最新的).

    选中行为对 agent 可见: 同 slug 多副本时显式 LOG.warning 列出所有候选 + 选了哪个,
    无候选时 LOG.warning 提示 (agent 才能感知 'slug 拼错' 或 'pipeline 没跑' 这类错).
    """
    candidates = []
    for p in workspace.iterdir():
        if not p.is_dir():
            continue
        # 形如 2026-08-24_<slug> 或 2026-08-24_<slug>_1 / _2
        parts = p.name.split("_", 1)
        if len(parts) != 2:
            continue
        name_part = parts[1]
        name_part = re.sub(r"_\d+$", "", name_part)
        if name_part != slug:
            continue
        candidates.append(p)

    if not candidates:
        LOG.warning("slug '%s' 在 %s 下无匹配 run folder (检查 pipeline.py 是否成功跑过该关键词, "
                    "或 --runs 拼写是否与 --keywords 一字不差)", slug, workspace)
        return []
    # 取最后一个 (按字典序, _2 > _1 > base)
    candidates.sort(key=lambda p: p.name)
    chosen = candidates[-1]
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        LOG.warning("slug '%s' 匹配到 %d 个 run folder: [%s]; 选中 '%s' (字典序最后)",
                    slug, len(candidates), names, chosen.name)
    else:
        LOG.info("slug '%s' -> run folder '%s'", slug, chosen.name)
    f = chosen / "enriched.jsonl"
    if not f.exists():
        LOG.warning("run folder '%s' 缺少 enriched.jsonl", chosen.name)
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


# ----------------------------- 聚合 -----------------------------


def aggregate(runs: List[str], dimensions: Dict[str, Set[str]], workspace: Path,
              top_notes: int = 10, top_comments: int = 10, full_notes: int = 5) -> dict:
    out: dict = {
        "by_dimension": {},
        "totals": {
            "runs": runs,
            "notes_total": 0,
            "comments_total": 0,
            "dimensions": sorted(dimensions),
        },
    }
    for dim, words in dimensions.items():
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
        top_n = sorted(dim_notes, key=lambda x: (x["liked"] or 0), reverse=True)[:top_notes]
        top_c = sorted(
            [c for c in dim_comments if not c["is_sub"]],
            key=lambda x: (x["liked"] or 0), reverse=True,
        )[:top_comments]
        full = sorted(
            [n for n in dim_notes if n["desc_plain"]],
            key=lambda x: (x["liked"] or 0), reverse=True,
        )[:full_notes]
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
                for n in top_n
            ],
            "top_comments_by_liked": [
                {
                    "comment_id": c["comment_id"], "run": c["run"],
                    "note_id": c["note_id"], "content": c["content"],
                    "liked": c["liked"], "user": c["user"],
                }
                for c in top_c
            ],
            "full_notes": [
                {
                    "note_id": n["note_id"], "run": n["run"], "title": n["title"],
                    "desc_plain": n["desc_plain"], "tags": n["tags"],
                    "liked": n["liked"], "comment_count": n["comment_count"],
                    "user": n["user"], "ts_iso": n["ts_iso"],
                    "share_url": n["share_url"],
                }
                for n in full
            ],
        }
        out["totals"]["notes_total"] += len(dim_notes)
        out["totals"]["comments_total"] += len(dim_comments)
    return out


# ----------------------------- CLI -----------------------------


def main(argv=None):
    p = argparse.ArgumentParser(description="跨 run 聚合分析 (维度由 agent 通过 --dimensions 定义)")
    p.add_argument("--runs", required=True,
                   help="逗号分隔的 run slug, 多个关键词的 --topic 值")
    p.add_argument("--dimensions", required=True,
                   help="自由定义的维度, 格式: 'name:kw1,kw2;name2:kw3,kw4'")
    p.add_argument("--workspace", default="data/runs",
                   help="workspace 根目录 (默认 data/runs)")
    p.add_argument("--output", default=None,
                   help="输出 JSON 路径, 默认 <workspace>/_cross_analyze.json")
    p.add_argument("--top-notes", type=int, default=10,
                   help="每个维度输出 Top N 笔记 (默认 10)")
    p.add_argument("--top-comments", type=int, default=10,
                   help="每个维度输出 Top N 评论 (默认 10)")
    p.add_argument("--full-notes", type=int, default=5,
                   help="每个维度输出带完整正文的笔记数 (默认 5)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    import logging
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(LOG_NAME)

    runs = [s.strip() for s in args.runs.split(",") if s.strip()]
    if not runs:
        print("参数错误: --runs 为空", file=sys.stderr)
        return 3
    try:
        dimensions = parse_dimensions(args.dimensions)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 3

    workspace = Path(args.workspace).resolve()
    output = Path(args.output) if args.output else workspace / "_cross_analyze.json"

    log.info("聚合 %d run × %d 维度 -> %s", len(runs), len(dimensions), output)
    for r in runs:
        log.info("  run: %s", r)
    for d in dimensions:
        log.info("  dimension: %s (%d 关键词)", d, len(dimensions[d]))

    result = aggregate(runs, dimensions, workspace,
                       top_notes=args.top_notes,
                       top_comments=args.top_comments,
                       full_notes=args.full_notes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("写入 %s", output)

    # 摘要
    print("\n=== 摘要 ===")
    thin_dims = []
    for dim, data in result["by_dimension"].items():
        n_count = data['notes_count']
        c_count = data['comments_count']
        line = f"  [{dim}] 笔记 {n_count} / 评论 {c_count}"
        if n_count == 0:
            line += "  *** 稀薄: 该维度无笔记命中 ***"
            thin_dims.append(dim)
        print(line)
        for n in data["top_notes_by_liked"][:3]:
            mark = "[正文]" if n["has_body"] else "[标题]"
            title = (n.get("title") or "").strip() or "(无标题)"
            print(f"    {mark} [{n['run']}/{n['note_id'][:8]}] {title[:50]} (赞 {n['liked']})")
    print(f"\n  total notes: {result['totals']['notes_total']}")
    print(f"  total comments: {result['totals']['comments_total']}")
    if thin_dims:
        print(f"\n  WARN: 维度 {thin_dims} 命中稀薄 (0 笔记). 建议补跑关键词或调整该维度的关键词集.",
              file=sys.stderr)
        return 2  # 退出码 2 = 数据稀薄, 但流程成功
    return 0


if __name__ == "__main__":
    sys.exit(main())