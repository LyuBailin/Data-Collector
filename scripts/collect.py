# -*- coding: utf-8 -*-
"""
collect.py - 数据采集 CLI

支持的抓取类型:
  --keyword / --pages       关键词搜索笔记
  --user / --pages          指定用户主页笔记
  --note / --with-comments  单篇笔记 (可选评论)
  --hotlist                 热门榜
  --search-user / --pages   搜索用户

输出: data/raw/<name>.jsonl, 每行一条 record = {endpoint, fetched_at, item, raw}
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from xhs_client import XHSClient, BASE_HOST  # noqa: E402

LOG = logging.getLogger("collect")


# -------------------------- 字段归一化 --------------------------

def _ts_iso(ms):
    if not ms:
        return None
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def _extract_tags(text):
    if not text:
        return []
    return [m.strip() for m in re.findall(r"#([^#\s]+)", text) if m.strip()]


def normalize_note(note):
    interact = note.get("interact_info") or note.get("interactInfo") or {}
    user = note.get("user") or note.get("user_info") or {}
    cover = ""
    cov = note.get("cover")
    if isinstance(cov, dict):
        cover = cov.get("url") or cov.get("url_default") or cov.get("url_pre") or ""
    elif isinstance(cov, str):
        cover = cov
    video = note.get("video") or {}
    stream = ((video.get("media") or {}).get("stream") or {}).get("h264") or []
    video_url = stream[0].get("master_url") if stream else ""
    share = note.get("share_info") or note.get("shareInfo") or {}
    return {
        "note_id": note.get("note_id") or note.get("id") or note.get("noteId") or "",
        "title": (note.get("title") or note.get("display_title") or "")[:200],
        "desc": (note.get("desc") or "")[:5000],
        "type": note.get("type") or note.get("note_type") or "normal",
        "user": {
            "user_id": user.get("user_id") or user.get("id") or "",
            "nickname": user.get("nickname") or user.get("nick_name") or "",
            "fans": user.get("fans") or user.get("fans_count") or 0,
            "red_official": bool(user.get("red_official") or user.get("official") or False),
        },
        "interact": {
            "liked": int(interact.get("liked_count") or interact.get("liked") or 0),
            "collected": int(interact.get("collected_count") or interact.get("collected") or 0),
            "comment": int(interact.get("comment_count") or interact.get("comment") or 0),
            "share": int(interact.get("share_count") or interact.get("shared_count") or interact.get("share") or 0),
        },
        "cover_url": cover,
        "video_url": video_url,
        "tags": note.get("tags") if isinstance(note.get("tags"), list)
        else _extract_tags(note.get("desc", "") or ""),
        "ts": note.get("time") or note.get("last_update_time") or note.get("lastUpdateTime") or 0,
        "ts_iso": _ts_iso(note.get("time") or note.get("last_update_time") or note.get("lastUpdateTime")),
        "ip_location": note.get("ip_location") or note.get("ipLocation") or "",
        "xsec_token": note.get("xsec_token") or note.get("xsecToken") or "",
        "share_url": (share.get("link") if isinstance(share, dict) else "") or "",
    }


def state_note_to_api(n):
    """把笔记详情页 INITIAL_STATE 里的 camelCase note 转成 normalize_note 认识的形状。"""
    if not n:
        return {}
    inter = n.get("interactInfo") or {}
    return {
        "note_id": n.get("noteId") or n.get("id"),
        "title": n.get("title"),
        "desc": n.get("desc"),
        "type": n.get("type"),
        "user": n.get("user") or {},
        "interact_info": {
            "liked_count": inter.get("likedCount"),
            "collected_count": inter.get("collectedCount"),
            "comment_count": inter.get("commentCount"),
            "share_count": inter.get("shareCount"),
        },
        "tags": [t.get("name") for t in (n.get("tagList") or []) if isinstance(t, dict) and t.get("name")],
        "time": n.get("time") or n.get("lastUpdateTime"),
        "ip_location": n.get("ipLocation"),
        "xsec_token": n.get("xsecToken"),
        "share_info": n.get("shareInfo") or {},
        "cover": {"url": ((n.get("imageList") or [{}])[0] or {}).get("urlDefault") or ""}
        if isinstance(n.get("imageList"), list) and n.get("imageList") else "",
    }


def normalize_comment(c, schema="v2"):
    """把 XHS 原始评论结构归一化。

    schema='v2' (推荐): /api/sns/web/v2/comment/page
       - c.user_info 包含用户信息
       - c.like_count 是字符串
       - c.sub_comments 是嵌套子评论数组
       - c.ip_location 是省份
    schema='v1' (旧版): /api/sns/web/v1/comment/page
       - c.user 直接是用户信息
       - c.like_count 是数字
    """
    if schema == "v2":
        ui = c.get("user_info") or {}
    else:
        ui = c.get("user") or {}

    def _to_int(v):
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "comment_id": c.get("id") or c.get("comment_id"),
        "content": (c.get("content") or "")[:2000],
        "liked": _to_int(c.get("like_count")),
        "ts": c.get("create_time") or c.get("time") or 0,
        "ts_iso": _ts_iso(c.get("create_time") or c.get("time")),
        "user": {
            "user_id": ui.get("user_id") or ui.get("id"),
            "nickname": ui.get("nickname") or ui.get("nick_name"),
        },
        "sub_count": _to_int(c.get("sub_comment_count")),
        "ip_location": c.get("ip_location", ""),
        "liked_by_me": bool(c.get("liked", False)),
        "sub_comments": [normalize_comment(s, schema="v2") for s in (c.get("sub_comments") or [])],
    }
def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def _read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def collect_search_notes(client, keyword, pages, page_size=20, sort="general"):
    if client.sign_engine == "browser":
        # 页面驱动: 搜索接口已迁移到 so.xiaohongshu.com v2, raw fetch 会被 406 拒
        return _collect_search_notes_page_driven(client, keyword, pages)

    rows = []
    for page in range(1, pages + 1):
        body = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": "",
            "sort": sort,
            "note_type": 0,
        }
        params = {"source": "web_search_result", "device_ratio": 1}
        ref = f"{BASE_HOST}/search_result?keyword={keyword}&source=web_search_result"
        resp = client.post("/api/sns/web/v1/search/notes", params=params, body=body, referer=ref)
        items = ((resp.get("data") or {}).get("items") or [])
        for raw in items:
            note = raw.get("note_card") or raw
            rows.append({
                "endpoint": "search/notes",
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "page": page,
                "item": normalize_note(note),
                "raw": raw,
            })
        LOG.info("search/notes keyword=%s page=%d 取得 %d, 累计 %d", keyword, page, len(items), len(rows))
        if not items:
            break
    return rows


def _collect_search_notes_page_driven(client, keyword, pages):
    """浏览器引擎专用: 让搜索页自己发 v2 search 请求, 拦截页面响应。"""
    from playwright_driver import ensure_loop, page_search_notes

    items = ensure_loop().run_until_complete(page_search_notes(keyword, pages=pages))
    rows = []
    for raw in items:
        note = raw.get("note_card") or raw
        if not (note.get("note_id") or note.get("id") or note.get("noteId")):
            note = {**note, "id": raw.get("id")}
        rows.append({
            "endpoint": "search/notes",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "page": None,  # 页面驱动: 由页面自动翻页, 不标记页码
            "item": normalize_note(note),
            "raw": raw,
        })
    LOG.info("page_search keyword=%s 共 %d 条", keyword, len(rows))
    return rows


def collect_user_notes(client, user_id, pages, cursor=""):
    rows = []
    for _ in range(pages):
        params = {"num": 30, "cursor": cursor, "user_id": user_id, "image_formats": "jpg,webp,avif"}
        resp = client.get("/api/sns/web/v1/user/posted", params=params)
        data = resp.get("data") or {}
        notes = data.get("notes") or []
        for raw in notes:
            rows.append({
                "endpoint": "user/posted",
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "item": normalize_note(raw),
                "raw": raw,
            })
        cursor = data.get("cursor") or ""
        has_more = bool(data.get("has_more", False))
        LOG.info("user/posted user_id=%s 取得 %d, cursor=%s...", user_id, len(notes), cursor[:10])
        if not has_more or not cursor:
            break
    return rows


def collect_note_detail(client, note_id, xsec_token=""):
    if client.sign_engine == "browser":
        return _collect_note_detail_page_driven(client, note_id, xsec_token)

    params = {"source_note_id": note_id}
    resp = client.get("/api/sns/web/v1/feed", params=params)
    rows = []
    items = (resp.get("data") or {}).get("items") or []
    for raw in items:
        note = raw.get("note_card") or raw
        if (note.get("note_id") or note.get("id")) != note_id:
            continue
        rows.append({
            "endpoint": "feed/note_detail",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "item": normalize_note(note),
            "raw": raw,
        })
    if not rows:
        LOG.warning("note_detail 未找到 note_id=%s, items=%d", note_id, len(items))
    return rows


def _collect_note_detail_page_driven(client, note_id, xsec_token):
    """浏览器引擎专用: 打开笔记页, 从 INITIAL_STATE 提取笔记正文。"""
    from playwright_driver import ensure_loop, page_note_detail

    result = ensure_loop().run_until_complete(page_note_detail(note_id, xsec_token=xsec_token, max_comment_pages=1))
    note = state_note_to_api(result["note"])
    return [{
        "endpoint": "feed/note_detail",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "item": normalize_note(note),
        "raw": result["note"],
    }]


def collect_comments(client, note_id, max_pages=3, schema="v2", fetch_sub=True, xsec_token=""):
    """抓取某条笔记的全部评论 (含子评论)。

    schema:
      - 'v2' (默认, 推荐): /api/sns/web/v2/comment/page, 响应里带 sub_comments 嵌套
      - 'v1' (旧版): /api/sns/web/v1/comment/page

    fetch_sub:
      - True (默认): 把嵌套的 sub_comments 也展开成独立记录 (endpoint = comment/page/sub)
      - False: 只保留顶层评论

    xsec_token: 浏览器引擎 (页面驱动) 下需要笔记的 xsec_token 才能加载评论。
    """
    if client.sign_engine == "browser":
        return _collect_comments_page_driven(client, note_id, max_pages, schema, fetch_sub, xsec_token)

    rows = []
    cursor = ""
    if schema == "v2":
        endpoint = "/api/sns/web/v2/comment/page"
    else:
        endpoint = "/api/sns/web/v1/comment/page"
    for _ in range(max_pages):
        params = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
        }
        resp = client.get(endpoint, params=params)
        data = resp.get("data") or {}
        comments = data.get("comments") or []
        for c in comments:
            norm = normalize_comment(c, schema=schema)
            rows.append({
                "endpoint": "comment/page",
                "schema": schema,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "note_id": note_id,
                "item": norm,
                "raw": c,
            })
            if fetch_sub and norm.get("sub_comments"):
                for sub in norm["sub_comments"]:
                    sub_item = dict(sub)
                    sub_item["parent_comment_id"] = norm.get("comment_id")
                    rows.append({
                        "endpoint": "comment/page/sub",
                        "schema": schema,
                        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "note_id": note_id,
                        "parent_comment_id": norm.get("comment_id"),
                        "item": sub_item,
                        "raw": c,
                    })
        cursor = data.get("cursor") or ""
        has_more = bool(data.get("has_more", False))
        LOG.info("%s note_id=%s 取得 %d, cursor=%s", endpoint, note_id, len(comments), cursor[:10])
        if not has_more or not cursor:
            break
    return rows


def _collect_comments_page_driven(client, note_id, max_pages, schema, fetch_sub, xsec_token):
    """浏览器引擎专用: 打开笔记页, 捕获页面自身发出的 v2/comment/page 响应。"""
    from playwright_driver import ensure_loop, page_note_detail

    result = ensure_loop().run_until_complete(
        page_note_detail(note_id, xsec_token=xsec_token, max_comment_pages=max_pages)
    )
    rows = []
    for c in result["comments"]:
        norm = normalize_comment(c, schema="v2")
        rows.append({
            "endpoint": "comment/page",
            "schema": "v2",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "note_id": note_id,
            "item": norm,
            "raw": c,
        })
        if fetch_sub and norm.get("sub_comments"):
            for sub in norm["sub_comments"]:
                sub_item = dict(sub)
                sub_item["parent_comment_id"] = norm.get("comment_id")
                rows.append({
                    "endpoint": "comment/page/sub",
                    "schema": "v2",
                    "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "note_id": note_id,
                    "parent_comment_id": norm.get("comment_id"),
                    "item": sub_item,
                    "raw": c,
                })
    LOG.info("page comment note_id=%s 共 %d 条评论", note_id, len(rows))
    return rows

def collect_hotlist(client, category="general", page_size=50):
    body = {"category": category, "page_size": page_size}
    params = {"source": "web_hot_rank"}
    resp = client.post("/api/sns/web/v1/search/hotlist", params=params, body=body, referer=f"{BASE_HOST}/explore")
    items = ((resp.get("data") or {}).get("items") or [])
    rows = []
    for raw in items:
        rows.append({
            "endpoint": "search/hotlist",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "item": {
                "query": raw.get("query") or raw.get("title") or "",
                "word_id": raw.get("word_id") or raw.get("id") or "",
                "score": raw.get("score") or raw.get("view_count") or 0,
                "category": category,
                "ts": raw.get("create_time") or raw.get("time") or 0,
                "ts_iso": _ts_iso(raw.get("create_time") or raw.get("time")),
            },
            "raw": raw,
        })
    LOG.info("search/hotlist category=%s 取得 %d", category, len(items))
    return rows


def collect_search_users(client, keyword, pages):
    rows = []
    for page in range(1, pages + 1):
        body = {"keyword": keyword, "page": page, "page_size": 20}
        resp = client.post("/api/sns/web/v1/search/users", params={"source": "web_search_result"}, body=body)
        items = ((resp.get("data") or {}).get("users") or [])
        for raw in items:
            rows.append({
                "endpoint": "search/users",
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "page": page,
                "item": {
                    "user_id": raw.get("user_id") or raw.get("id"),
                    "nickname": raw.get("nickname") or raw.get("nick_name"),
                    "red_id": raw.get("red_id") or raw.get("red_official_id"),
                    "fans": raw.get("fans") or raw.get("fans_count"),
                    "notes": raw.get("notes") or 0,
                    "description": (raw.get("desc") or "")[:500],
                },
                "raw": raw,
            })
        LOG.info("search/users keyword=%s page=%d 取得 %d", keyword, page, len(items))
        if not items:
            break
    return rows


# -------------------------- CLI --------------------------

def _build_client(args):
    return XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        sign_engine=args.sign_engine,
    )


def main(argv=None):
    p = argparse.ArgumentParser(description="小红书数据采集")
    p.add_argument("--cookie-file", default="assets/cookies.json")
    p.add_argument("--fp-cache", default="assets/fingerprint.json")
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.0)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--sign-engine", choices=["legacy", "node", "browser"], default="browser")
    p.add_argument("--out", required=True, help="输出 JSONL 路径")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keyword", help="关键词搜索笔记")
    mode.add_argument("--user", help="用户 id, 抓取用户主页笔记")
    mode.add_argument("--note", help="单篇笔记 id")
    mode.add_argument("--hotlist", action="store_true", help="抓取热门榜")
    mode.add_argument("--search-user", help="按关键词搜索用户")
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--sort", default="general", help="搜索排序: general | popular | time_descending")
    p.add_argument("--category", default="general", help="hotlist 分类")
    p.add_argument("--with-comments", action="store_true", help="(配合 --note) 同时抓评论")
    p.add_argument("--max-comment-pages", type=int, default=3)
    p.add_argument("--xsec-token", default="", help="(配合 --note) 笔记访问令牌, 从笔记 URL ?xsec_token= 复制")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    client = _build_client(args)
    client.load()

    rows = []
    try:
        if args.keyword:
            rows = collect_search_notes(client, args.keyword, args.pages, args.page_size, args.sort)
        elif args.user:
            rows = collect_user_notes(client, args.user, args.pages)
        elif args.note:
            rows = collect_note_detail(client, args.note, xsec_token=args.xsec_token)
            if args.with_comments:
                rows += collect_comments(client, args.note, args.max_comment_pages, schema="v2",
                                         xsec_token=args.xsec_token)
        elif args.hotlist:
            rows = collect_hotlist(client, args.category, args.page_size)
        elif args.search_user:
            rows = collect_search_users(client, args.search_user, args.pages)
    finally:
        # 关闭 Playwright 浏览器单例, 避免 Chromium 子进程残留
        try:
            from playwright_driver import shutdown_now
            shutdown_now()
        except Exception as exc:
            LOG.warning("关闭浏览器单例失败: %s", exc)

    n = _write_jsonl(Path(args.out), rows)
    print(f"已写入 {args.out}: {n} 条记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())