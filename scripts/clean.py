# -*- coding: utf-8 -*-
"""
clean.py - 数据清洗

输入: data/raw/*.jsonl
输出: data/clean/*.jsonl

清洗动作:
  1. 剥离 emoji / 不可见字符 / 控制字符
  2. 规范化空白与换行
  3. 从 desc 中抽取纯净文本 (去 #tag 标记, 但保留 tag 列表)
  4. 数值字段 (liked / collected / comment / share) 归一为 int
  5. 计算互动率 engagement_rate = (liked + collected + comment) / (fans + 1000)
  6. 计算 desc 字数 (CJK + latin)
  7. 解析时间 (ms -> ISO)
  8. 按 note_id 去重
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("clean")


# ------------------------- 正则 / 字符集 -------------------------

_EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F2FF\u2600-\u26FF]"
)
_ZW_PATTERN = re.compile("[\u200B-\u200F\u202A-\u202E\u2060\uFEFF]")
_CTRL_PATTERN = re.compile("[\u0000-\u0008\u000B\u000C\u000E-\u001F]")
_HTML_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_WS = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\n{3,}")
_HASHTAG = re.compile(r"#([^#\s]+)")


def strip_emoji(text):
    if not text:
        return ""
    text = _EMOJI_PATTERN.sub("", text)
    text = _ZW_PATTERN.sub("", text)
    text = _CTRL_PATTERN.sub("", text)
    return text


def clean_text(text):
    if not text:
        return ""
    t = str(text)
    t = _HTML_TAG.sub(" ", t)
    t = strip_emoji(t)
    t = unicodedata.normalize("NFKC", t)
    t = _WS.sub(" ", t)
    t = _NEWLINES.sub("\n\n", t)
    return t.strip()


def desc_plain(desc, tags):
    if not desc:
        return ""
    t = desc
    t = _HTML_TAG.sub(" ", t)
    t = strip_emoji(t)
    t = unicodedata.normalize("NFKC", t)
    for tag in tags:
        t = t.replace("#" + tag, " ")
        t = t.replace("#" + tag + " ", " ")
        t = t.replace(" #" + tag, " ")
    t = re.sub(r"#\S+", " ", t)
    t = _WS.sub(" ", t)
    t = _NEWLINES.sub("\n\n", t)
    return t.strip()


def word_count(text):
    if not text:
        return 0
    zh = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    en = len(re.findall(r"[A-Za-z]+", text))
    return zh + en


def engagement_rate(item):
    interact = item.get("interact") or {}
    fans = (item.get("user") or {}).get("fans") or 0
    base = (interact.get("liked") or 0) + (interact.get("collected") or 0) + (interact.get("comment") or 0)
    denom = max(int(fans), 1000)
    return round(base / denom, 6)


# ------------------------- 主流程 -------------------------

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
                LOG.warning("JSON 解析失败: %s | %s", exc, line[:80])
    return rows


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def clean_record(rec):
    """把单条 raw record 转成 clean record。

    根据 endpoint 分派:
      - 笔记 (search/notes, user/posted, feed/note_detail 等): 走 desc/title 清洗
      - 评论 (comment/page, comment/page/sub): 走 content 清洗, 没有 desc/title
      - 热门榜 (search/hotlist): 保留 word_id / query / score, 不参与笔记清洗
      - 用户搜索 (search/users): 保留用户信息, 不参与笔记清洗
    """
    item = rec.get("item") or {}
    endpoint = rec.get("endpoint") or ""

    if endpoint == "search/hotlist":
        return {
            "endpoint": endpoint,
            "fetched_at": rec.get("fetched_at"),
            "is_hotlist": True,
            "word_id": item.get("word_id") or "",
            "query": clean_text(item.get("query") or ""),
            "score": int(item.get("score") or 0),
            "category": item.get("category") or "",
            "ts": item.get("ts") or 0,
            "ts_iso": item.get("ts_iso"),
        }

    if endpoint == "search/users":
        return {
            "endpoint": endpoint,
            "fetched_at": rec.get("fetched_at"),
            "is_user": True,
            "user_id": item.get("user_id"),
            "nickname": clean_text(item.get("nickname") or ""),
            "red_id": item.get("red_id") or "",
            "fans": int(item.get("fans") or 0),
            "notes": int(item.get("notes") or 0),
            "description": clean_text(item.get("description") or ""),
        }

    is_comment = endpoint.startswith("comment/")
    is_note = endpoint in ("search/notes", "user/posted", "feed/note_detail")

    if is_comment:
        # --- 评论清洗 ---
        content = clean_text(item.get("content", ""))
        word_count_content = word_count(content)
        return {
            "endpoint": endpoint,
            "schema": rec.get("schema"),
            "fetched_at": rec.get("fetched_at"),
            "page": rec.get("page"),
            "note_id": rec.get("note_id") or item.get("note_id"),
            "is_comment": True,
            "is_sub_comment": endpoint == "comment/page/sub",
            "parent_comment_id": rec.get("parent_comment_id") or item.get("parent_comment_id"),
            "comment_id": item.get("comment_id"),
            "content": content,
            "word_count": word_count_content,
            "liked": int(item.get("liked") or 0),
            "ts": item.get("ts") or 0,
            "ts_iso": item.get("ts_iso"),
            "ip_location": clean_text(item.get("ip_location") or ""),
            "liked_by_me": bool(item.get("liked_by_me", False)),
            "user": {
                "user_id": (item.get("user") or {}).get("user_id"),
                "nickname": clean_text((item.get("user") or {}).get("nickname") or ""),
            },
            "sub_count": int(item.get("sub_count") or 0),
        }

    if not is_note:
        # 未知类型, 保守地做最小清洗
        return {
            "endpoint": endpoint,
            "fetched_at": rec.get("fetched_at"),
            "item": item,
        }

    # --- 笔记清洗 (search/notes, user/posted, feed/note_detail, hotlist) ---
    desc = item.get("desc") or ""
    title = item.get("title") or ""
    tags = item.get("tags") or []

    cleaned_title = clean_text(title)
    cleaned_desc = desc_plain(desc, tags)
    word_count_desc = word_count(cleaned_desc)
    word_count_title = word_count(cleaned_title)
    tags_norm = list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))

    return {
        "endpoint": endpoint,
        "fetched_at": rec.get("fetched_at"),
        "page": rec.get("page"),
        "note_id": item.get("note_id") or rec.get("note_id"),
        "is_comment": False,
        "title": cleaned_title,
        "desc_plain": cleaned_desc,
        "tags": tags_norm,
        "type": item.get("type") or "normal",
        "user": {
            "user_id": (item.get("user") or {}).get("user_id"),
            "nickname": clean_text((item.get("user") or {}).get("nickname") or ""),
            "fans": int((item.get("user") or {}).get("fans") or 0),
            "red_official": bool((item.get("user") or {}).get("red_official")),
        },
        "interact": {
            "liked": int((item.get("interact") or {}).get("liked") or 0),
            "collected": int((item.get("interact") or {}).get("collected") or 0),
            "comment": int((item.get("interact") or {}).get("comment") or 0),
            "share": int((item.get("interact") or {}).get("share") or 0),
        },
        "engagement_rate": engagement_rate(item),
        "word_count": word_count_desc,
        "title_word_count": word_count_title,
        "ts": item.get("ts") or 0,
        "ts_iso": item.get("ts_iso"),
        "ip_location": clean_text(item.get("ip_location") or ""),
        "share_url": item.get("share_url") or "",
        "cover_url": item.get("cover_url") or "",
        "video_url": item.get("video_url") or "",
    }

def clean_records(records):
    """清洗 + 去重 + 过滤无效条目。

    去重键:
      - 笔记: note_id
      - 评论 (含子评论): comment_id
      - 热门榜: word_id
      - 用户搜索: user_id
    """
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for rec in records:
        c = clean_record(rec)
        if c.get("is_comment"):
            key = c.get("comment_id")
        elif c.get("is_hotlist"):
            key = c.get("word_id")
        elif c.get("is_user"):
            key = c.get("user_id")
        else:
            key = c.get("note_id")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(c)
    return cleaned


def main(argv=None):
    p = argparse.ArgumentParser(description="小红书数据清洗")
    p.add_argument("--in", dest="src", required=True, help="输入 JSONL")
    p.add_argument("--out", required=True, help="输出 JSONL")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    src = Path(args.src)
    records = _read_jsonl(src)
    LOG.info("读入 %d 条原始记录", len(records))

    cleaned = clean_records(records)
    n = _write_jsonl(Path(args.out), cleaned)
    LOG.info("输出 %d 条清洗记录 -> %s", n, args.out)
    print(f"已写入 {args.out}: {n} 条 (输入 {len(records)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())