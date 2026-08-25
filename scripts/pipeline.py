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
    collect_note_full,
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
    """slug 化: 把任意字符串转成 [A-Za-z0-9_], 保留 ASCII 大小写.

    历史: 之前用 .lower() 把 ASCII 转小写, 但导致 --keywords 'AI神器' 生成的
    folder 名 (2026-08-25_ai神器) 与 --runs 'AI神器' 拼写不一致, 跨 run
    聚合时匹配失败. 现在保留原大小写, 让 --keywords / --runs / --dimensions
    的 slug 字面一致 (XHS 模糊搜索场景下 'AI神器' 与 'ai神器' 实际命中笔记
    可能不同, 但保证三个 CLI 参数用同一字符串时一致匹配).

    中文 isalnum() 返回 True 但没有大小写概念, 自动保留.
    """
    if not s:
        return "default"
    out = []
    for ch in str(s):
        if ch.isalnum():
            out.append(ch)
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

    try:
        n_csv = analyze_mod.export_notes_csv(enriched_records, run_dir / "notes.csv")
        LOG.info("notes.csv: %d -> %s", n_csv, run_dir / "notes.csv")
        if any(r.get("is_comment") for r in enriched_records):
            n_c2 = analyze_mod.export_comments_csv(enriched_records, run_dir / "comments.csv")
            LOG.info("comments.csv: %d -> %s", n_c2, run_dir / "comments.csv")
    except Exception as exc:
        LOG.warning("CSV 导出失败: %s", exc)
    return 0


def _enrich_top_notes(client, rows, n, with_comments):
    """给热度 Top N 的搜索卡片补全正文/标签/时间, 可选评论; 返回扩展后的 rows。

    热度 = liked*1 + collected*3 + comment*2 (与 enrich.heat_score 权重一致)。
    补全用 collect_note_full (页面驱动, 一次导航拿笔记+评论)。

    n 的语义:
      n > 0 : 补全热度 Top n
      n == 0: 不补全
      n < 0 : 补全全部
    """
    notes = [r for r in rows if (r.get("item") or {}).get("note_id")]
    if not notes:
        LOG.warning("无笔记可补全")
        return rows
    if n == 0:
        return rows

    def _score(r):
        it = (r.get("item") or {}).get("interact") or {}
        return int(it.get("liked") or 0) * 1 + int(it.get("collected") or 0) * 3 + int(it.get("comment") or 0) * 2

    ranked = sorted(notes, key=_score, reverse=True)
    if n > 0:
        ranked = ranked[:n]
    else:
        LOG.info("补全全部 %d 条 (n<0 触发)", len(ranked))
    target_ids = [r["item"]["note_id"] for r in ranked]
    LOG.info("补全 Top %d 笔记详情: %s", len(target_ids), target_ids)

    enriched = []
    for r in rows:
        nid = (r.get("item") or {}).get("note_id")
        if nid not in target_ids:
            enriched.append(r)
            continue
        xsec = (r.get("item") or {}).get("xsec_token") or ""
        try:
            details = collect_note_full(client, nid, xsec_token=xsec,
                                        with_comments=with_comments, max_comment_pages=1)
        except Exception as exc:
            LOG.warning("补全 %s 失败: %s", nid, exc)
            enriched.append(r)
            continue
        if not details:
            LOG.warning("补全 %s: 无详情, 保留搜索卡片", nid)
            enriched.append(r)
            continue
        merged = dict(r)
        merged["item"] = details[0]["item"]
        merged["detail_enriched"] = True
        enriched.append(merged)
        if with_comments:
            enriched.extend(details[1:])
        LOG.info("补全 %s OK (新增记录 %d)", nid, len(details))
    LOG.info("补全结束: %d -> %d 条 (含评论)", len(rows), len(enriched))
    return enriched


def run_keyword_for(args, client, workspace, keyword, topic_override=None):
    """跑单个关键词的完整 pipeline。

    topic_override:
      - None: 用 args.topic (单关键词模式, 向后兼容) 或 keyword 自身
      - 非 None: 显式指定 run folder 后缀 (批量模式用 keyword slug 派生)
    """
    topic = topic_override if topic_override is not None else (args.topic or keyword)
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_search_notes(client, keyword, args.pages, args.page_size, args.sort)
    if args.enrich_notes and rows:
        rows = _enrich_top_notes(client, rows, args.enrich_notes, args.with_comments)
    return _full_pipeline(run_dir, rows, label=f"keyword={keyword}")


def run_keywords_batch(args, client, workspace):
    """同进程串行跑多个关键词 (--keywords 模式)。

    兑现 SKILL §2 Step 4 契约:
      - 复用同一个 client (Chromium + cookie 单例), 不并发
      - 每个关键词独立 run folder (topic 从 keyword 自动 slug, --topic 在批量模式下被忽略)
      - 单关键词失败不中断批次: try/except 包裹, 记录日志后继续下一个
      - 整体 rc: 任一关键词失败 -> 1; 全部成功 -> 0
    """
    kws = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    if not kws:
        LOG.error("--keywords 为空或全为空白")
        return 1
    LOG.info("批量关键词模式: %d 个 -> 同进程串行", len(kws))
    overall_rc = 0
    for i, kw in enumerate(kws, 1):
        LOG.info("=" * 60)
        LOG.info("关键词 [%d/%d]: %s", i, len(kws), kw)
        LOG.info("=" * 60)
        # 批量模式下 topic 强制 = keyword 自身 (slug 派生 run folder 名),
        # 与 docstring "topic 从 keyword 自动 slug, --topic 在批量模式下被忽略" 一致.
        try:
            run_keyword_for(args, client, workspace, kw, topic_override=kw)
        except Exception as exc:
            LOG.error("关键词 '%s' 失败, 跳过继续下一个: %s", kw, exc)
            overall_rc = 1
    LOG.info("批量关键词结束: rc=%d", overall_rc)
    return overall_rc


def run_user(args, client, workspace):
    topic = args.topic or f"user_{args.user}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_user_notes(client, args.user, args.pages)
    return _full_pipeline(run_dir, rows, label=f"user={args.user}")


def run_note(args, client, workspace):
    topic = args.topic or f"note_{args.note}"
    run_dir = _make_run_dir(workspace, topic)
    rows = collect_note_detail(client, args.note, xsec_token=args.xsec_token)
    if args.with_comments:
        rows += collect_comments(client, args.note, args.max_comment_pages, schema="v2",
                                 xsec_token=args.xsec_token)
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
    p.add_argument("--with-comments", action="store_true", help="(配合 --note / --enrich-notes) 同时抓评论")
    p.add_argument("--max-comment-pages", type=int, default=3)
    p.add_argument("--xsec-token", default="", help="(配合 --note) 笔记访问令牌, 从笔记 URL ?xsec_token= 复制")
    p.add_argument("--enrich-notes", type=int, default=-1, metavar="N",
                   help="(配合 --keyword / --keywords) 对热度 Top N 的笔记补全正文/标签/时间 (可选 --with-comments). "
                        "N>0: Top N; N=0: 不补全; N<0 (默认 -1): 补全全部. "
                        "搜索接口返回的 note_card.desc 通常为空字符串, 要拿到正文必须显式补全.")
    p.add_argument("--log-level", default="INFO")
    # 关键词模式: --keyword (单) 与 --keywords (多, 逗号分隔) 二选一; 批量模式同进程串行, 复用 Chromium/cookie 单例
    kw = p.add_mutually_exclusive_group()
    kw.add_argument("--keyword", help="单关键词搜索笔记 (例: --keyword 'hc 缩减')")
    kw.add_argument("--keywords", help="多关键词 (逗号分隔), 同进程串行跑, 复用 Chromium/cookie 单例 "
                    "(例: --keywords 'hc 缩减,面试经验,大厂避雷')")
    # 其他模式: 四选一 (与上面 kw 组互斥, 由 main 校验 '恰好一个')
    mode = p.add_mutually_exclusive_group()
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

    # 互斥校验: keyword/keywords/user/note/hotlist/search-user 恰好一个
    non_kw_mode = [args.user, args.note, args.hotlist, args.search_user]
    n_kw = sum(1 for x in [args.keyword, args.keywords] if x)
    n_mode = sum(1 for x in non_kw_mode if x)
    if args.keyword and args.keywords:
        LOG.error("--keyword 与 --keywords 互斥, 二选一")
        return 2
    if n_kw + n_mode != 1:
        LOG.error("必须且只能选择一个模式: --keyword / --keywords / --user / --note / --hotlist / --search-user")
        return 2

    rc = 0
    try:
        if args.keyword:
            rc = run_keyword_for(args, client, workspace, args.keyword)
        elif args.keywords:
            rc = run_keywords_batch(args, client, workspace)
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
