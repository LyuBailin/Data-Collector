# -*- coding: utf-8 -*-
"""
pipeline.py - 一键端到端 pipeline

按顺序执行:
  collect -> clean -> enrich -> analyze

每次跑会创建一个新的 run folder:
  <workspace>/<YYYY-MM-DD>_<topic>/
    raw.jsonl
    clean.jsonl
    enriched.jsonl
    report.md
    summary.json

支持子命令:
  - keyword   关键词搜索笔记
  - user      用户主页笔记
  - note      单篇笔记 (配合 --with-comments 拉评论)
  - hotlist   热门榜
  - search-user  关键词搜用户

示例:
  python pipeline.py --keyword 露营 --pages 3 --topic camping
  python pipeline.py --note <note_id> --with-comments --topic 27jiuzi
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from collect import (  # noqa: E402
    collect_search_notes,
    collect_user_notes,
    collect_note_detail,
    collect_comments,
    collect_hotlist,
    collect_search_users,
)
from xhs_client import XHSClient  # noqa: E402

import clean as clean_mod  # noqa: E402
import enrich as enrich_mod  # noqa: E402
import analyze as analyze_mod  # noqa: E402

LOG = logging.getLogger("pipeline")


def _slugify(s):
    """简单 slug 化: 把任意字符串转成 [a-z0-9_]"""
    if not s:
        return "default"
    out = []
    for ch in str(s):
        if ch.isalnum():
            out.append(ch.lower())
        elif ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    return ("".join(out) or "default")[:60]


def _make_run_dir(workspace: Path, topic: str) -> Path:
    """创建 <workspace>/<YYYY-MM-DD>_<topic>/ 目录, 返回路径"""
    today = dt.date.today().isoformat()
    slug = _slugify(topic)
    run_dir = workspace / f"{today}_{slug}"
    i = 1
    while run_dir.exists():
        run_dir = workspace / f"{today}_{slug}_{i}"
        i += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path, rows):
    path = Path(path)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _write_json(path, data):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_text(path, text):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        f.write(text)


def _full_pipeline(run_dir: Path, rows: list, label: str = "") -> int:
    """在 run_dir 跑 clean / enrich / analyze。返回 0 成功, 1 失败。"""
    if not rows:
        LOG.warning("未抓到任何 %s, 中断", label or "数据")
        return 1

    raw_path = run_dir / "raw.jsonl"
    clean_path = run_dir / "clean.jsonl"
    enriched_path = run_dir / "enriched.jsonl"
    summary_path = run_dir / "summary.json"
    report_path = run_dir / "report.md"

    _write_jsonl(raw_path, rows)
    LOG.info("raw: %d -> %s", len(rows), raw_path)

    raw_records = _read_jsonl(raw_path)
    cleaned = clean_mod.clean_records(raw_records)
    if not cleaned:
        LOG.warning("清洗后无有效记录 (raw=%d), 中断", len(raw_records))
        return 1
    _write_jsonl(clean_path, cleaned)
    LOG.info("clean: %d -> %s", len(cleaned), clean_path)

    enriched_records = enrich_mod.enrich_records(cleaned)
    _write_jsonl(enriched_path, enriched_records)
    LOG.info("enrich: %d -> %s", len(enriched_records), enriched_path)

    summary = analyze_mod.aggregate(enriched_records)
    _write_json(summary_path, summary)
    LOG.info("summary: %s", summary_path)

    md = analyze_mod.render_markdown(summary, str(enriched_path))
    _write_text(report_path, md)
    LOG.info("report: %s", report_path)
    return 0


def run_keyword(args, client, workspace):
    topic = args.topic or args.keyword
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_search_notes(client, args.keyword, args.pages, args.page_size, args.sort)
    return _full_pipeline(run_dir, rows, label=f"keyword={args.keyword}")


def run_user(args, client, workspace):
    topic = args.topic or f"user_{args.user}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_user_notes(client, args.user, args.pages)
    return _full_pipeline(run_dir, rows, label=f"user={args.user}")


def run_note(args, client, workspace):
    topic = args.topic or f"note_{args.note}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_note_detail(client, args.note)
    if args.with_comments:
        rows += collect_comments(client, args.note, args.max_comment_pages, schema="v2")
    return _full_pipeline(run_dir, rows, label=f"note={args.note}")


def run_hotlist(args, client, workspace):
    topic = args.topic or f"hotlist_{args.category}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_hotlist(client, args.category, args.page_size)
    return _full_pipeline(run_dir, rows, label=f"hotlist={args.category}")


def run_search_user(args, client, workspace):
    topic = args.topic or f"search_user_{args.search_user}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_search_users(client, args.search_user, args.pages)
    return _full_pipeline(run_dir, rows, label=f"search_user={args.search_user}")


def build_parser():
    p = argparse.ArgumentParser(description="小红书 pipeline (collect+clean+enrich+analyze)")
    p.add_argument("--cookie-file", default="assets/cookies.json")
    p.add_argument("--fp-cache", default="assets/fingerprint.json")
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.0)
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--sort", default="general")
    p.add_argument("--category", default="general")
    p.add_argument("--workspace", default="data/runs",
                   help="run folder 根目录 (默认 data/runs, 每次跑会创建 <workspace>/<date>_<topic>/)")
    p.add_argument("--topic", help="run folder 名字后缀, 默认用 keyword / user / note / category 推断")
    p.add_argument("--sign-engine", choices=["legacy", "node", "browser"], default="browser")
    p.add_argument("--with-comments", action="store_true")
    p.add_argument("--max-comment-pages", type=int, default=3)
    p.add_argument("--log-level", default="INFO")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keyword", help="关键词搜索笔记")
    mode.add_argument("--user", help="用户主页笔记")
    mode.add_argument("--note", help="单篇笔记 (可选 --with-comments)")
    mode.add_argument("--hotlist", action="store_true", help="热门榜")
    mode.add_argument("--search-user", help="关键词搜用户")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    client = XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        sign_engine=args.sign_engine,
    )
    client.load()

    LOG.info("pipeline 启动 @ %s, workspace=%s", dt.datetime.now(dt.timezone.utc).isoformat(), workspace)
    rc = 0
    try:
        if args.keyword:
            rc = run_keyword(args, client, workspace)
        elif args.user:
            rc = run_user(args, client, workspace)
        elif args.note:
            rc = run_note(args, client, workspace)
        elif args.hotlist:
            rc = run_hotlist(args, client, workspace)
        elif args.search_user:
            rc = run_search_user(args, client, workspace)
        LOG.info("pipeline 结束 rc=%d", rc)
        return rc
    finally:
        # 关闭 Playwright 浏览器单例, 避免 Chromium 子进程残留
        try:
            from playwright_driver import shutdown_now
            shutdown_now()
        except Exception as exc:
            LOG.warning("关闭浏览器单例失败: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
