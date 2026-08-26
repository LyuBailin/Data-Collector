# -*- coding: utf-8 -*-
"""
cross_analyze.py - 跨 run 聚合分析 (维度按 slug 划分)

agent 完成多关键词 pipeline 跑完后, 调用此脚本把多个 run 的 enriched.jsonl
按 agent 定义的维度聚合, 输出结构化 JSON 给 agent 写报告时直接读。

设计原则:
  - 维度不内置、不硬编码。agent 根据与用户讨论产出的调研方向, 自由设计
    维度与对应关键词集, 通过 --dimensions 一次性传入。
  - **dim 命中按 slug 划分, 不做文本匹配**: XHS 搜索是模糊的 (搜 'AI神器'
    返回的笔记里 99% 不含 'AI神器' 三个字), 旧按 title/desc 文本匹配会
    大量 0 命中. 新版按 _source_slug 划分 — agent 在 Step 2 给每个维度
    配的 --keywords 就是 pipeline 实际跑的关键词, 对应的 slug 就是
    cross_analyze 命中的范围. 脚本只做 'slug 划分 → 按赞数排序 → 落盘'
    的纯工具, 不做语义聚类.

CLI:
  python scripts/cross_analyze.py \\
      --runs pants183,daman,pangnansheng \\
      --dimensions "搭配:pants183;品牌:daman,pangnansheng" \\
      --workspace data/runs \\
      --output data/runs/_183chuan.json

--dimensions 格式: "维度名:slug1,slug2;维度名2:slug3,slug4"
  - 维度名: 短 ASCII 标识 (如 fit / brand), 作为报告章节标题
  - slug: 与 --keywords / --runs 一字不差 (ASCII 大小写敏感, 中文原样)
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
    """解析 'name:slug1,slug2;name2:slug3,slug4' -> {name: {slug1, slug2, ...}}.

    解析格式未变 (name:值1,值2;...), 但语义是 **slug 集合** 不是关键词:
      agent 在 SKILL.md Step 2 给每个维度配的搜索关键词 (--keywords), 就是
      pipeline 实际跑的关键词, → 对应 run folder 的 slug. cross_analyze
      把这些 slug run 里的所有笔记归到该维度 (按 _source_slug 划分, 不做
      title/desc 文本匹配 — XHS 模糊搜索下文本匹配 99% 失配).

    任何段不合法 (缺冒号 / 空维度名 / 空 slug / 维度名重复) 都直接抛错,
    由调用方在参数解析期 fail loud, 不静默跳过。
    """
    out: Dict[str, Set[str]] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"维度段 '{chunk}' 缺冒号, 应为 '维度名:slug1,slug2'")
        name, slugs = chunk.split(":", 1)
        name = name.strip()
        slug_set = {s.strip() for s in slugs.split(",") if s.strip()}
        if not name:
            raise ValueError(f"维度段 '{chunk}' 维度名为空")
        if not slug_set:
            raise ValueError(f"维度段 '{chunk}' slug 为空")
        if name in out:
            raise ValueError(f"维度名重复: '{name}'")
        out[name] = slug_set
    if not out:
        raise ValueError("--dimensions 为空, 至少需要一个 '维度名:slug1,slug2' 段")
    return out


# ----------------------------- 命中与加载 -----------------------------


def _load_run(slug: str, workspace: Path) -> List[dict]:
    """加载单个 run folder 的 enriched.jsonl. 支持同 slug 的多个副本 (取最新的).

    选中行为对 agent 可见: 同 slug 多副本时显式 LOG.warning 列出所有候选 + 选了哪个,
    无候选时 LOG.warning 提示 (agent 才能感知 'slug 拼错' 或 'pipeline 没跑' 这类错).

    注入 _source_slug: 每条 record 加一个字段标记自己的源 slug, 供
    _notes_by_dim / _comments_by_dim 按 slug 划分维度命中.
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
                    "或 --runs 拼写是否与 --keywords 字面一致 — ASCII 大小写敏感)", slug, workspace)
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
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec["_source_slug"] = slug  # 注入源 slug, 让 _notes_by_dim 按 slug 划分
        out.append(rec)
    return out


def _notes_by_dim(records: List[dict], dim_slugs: Set[str]) -> List[dict]:
    """按 slug 划分命中: record._source_slug 在 dim_slugs 即命中该维度.

    设计原因 (commit 5d33c40 之后的实测发现):
      旧实现按 title+desc 含 dim 关键词做文本匹配. XHS 搜索是模糊的,
      搜 'AI神器' 返回的笔记里 99% 不含 'AI神器' 三个字 (XHS 按相关性返回
      所有 AI 工具类笔记), 旧匹配会得 0 命中. 改成按 slug 划分后, agent
      在 Step 2 设计的每个 dim 自然对应一组 --keywords (即 slugs),
      cross_analyze 把这些 slug run 里的笔记全归到该 dim. 与 XHS 实际
      返回一致.
    """
    out = []
    for r in records:
        if r.get("is_comment"):
            continue
        if r.get("_source_slug") not in dim_slugs:
            continue
        out.append({
            "note_id": r.get("note_id"),
            "title": r.get("title") or "(无标题)",
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


def _comments_by_dim(records: List[dict], dim_slugs: Set[str]) -> List[dict]:
    """按 slug 划分命中 (同 _notes_by_dim, 见其 docstring 设计原因)."""
    out = []
    for r in records:
        if not r.get("is_comment"):
            continue
        if r.get("_source_slug") not in dim_slugs:
            continue
        out.append({
            "note_id": r.get("note_id"),
            "comment_id": r.get("comment_id"),
            "content": r.get("content") or "",
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
    """跨 run 聚合.

    dimensions 格式: {dim_name: {slug1, slug2, ...}}. dim 命中 = 该 dim 的
    slug 集合里的所有 run 的所有记录 (按 slug 划分, 不再做文本匹配 — 见
    _notes_by_dim docstring).
    """
    # 一次加载所有 run 的 records (避免 dim 循环里 N×M 次重读 + 重复 inject _source_slug)
    per_slug_records: Dict[str, List[dict]] = {}
    for slug in runs:
        per_slug_records[slug] = _load_run(slug, workspace)

    out: dict = {
        "by_dimension": {},
        "totals": {
            "runs": runs,
            "notes_total": 0,
            "comments_total": 0,
            "dimensions": sorted(dimensions),
        },
    }
    for dim, dim_slugs in dimensions.items():
        dim_notes: List[dict] = []
        dim_comments: List[dict] = []
        per_run: Dict[str, dict] = {}
        for slug in runs:
            records = per_slug_records.get(slug, [])
            notes_in = [r for r in records if not r.get("is_comment") and r.get("_source_slug") in dim_slugs]
            comments_in = [r for r in records if r.get("is_comment") and r.get("_source_slug") in dim_slugs]
            per_run[slug] = {
                "records_total": len(records),
                "notes_matched": len(notes_in),
                "comments_matched": len(comments_in),
            }
            for n in _notes_by_dim(records, dim_slugs):
                n["run"] = slug
                dim_notes.append(n)
            for c in _comments_by_dim(records, dim_slugs):
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
                   help="逗号分隔的 run slug, 与 pipeline --keywords 一字不差")
    p.add_argument("--dimensions", required=True,
                   help="维度按 slug 划分, 格式: 'name:slug1,slug2;name2:slug3,slug4'. "
                        "slug 必须与 --runs 中的字符串一致")
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
        print(f"\n  WARN: 维度 {thin_dims} 命中稀薄 (0 笔记). 通常是该 dim 的 slug "
              f"在 --runs 里没对应 run folder (slug 拼写错, 或对应 pipeline 没跑过).",
              file=sys.stderr)
        return 2  # 退出码 2 = 数据稀薄, 但流程成功
    return 0


if __name__ == "__main__":
    sys.exit(main())