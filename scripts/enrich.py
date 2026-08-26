# -*- coding: utf-8 -*-
"""
enrich.py - 数据增强

输入: data/clean/*.jsonl
输出: data/enriched/*.jsonl

每个笔记附加:
  - keywords:    jieba.analyse.extract_tags 抽取的关键词 (TF-IDF, top 10)
  - topics:      jieba.analyse.textrank 抽取的关键短语 (TextRank, graph-based, top 5)
                 历史: 旧版 'topics' 字段是 normalize_topics(tags), 实际就是 tags 去空格版,
                 agent 看 report.md 困惑. 改用 textrank 后, keywords (TF-IDF 单高频词) 与
                 topics (TextRank 关键短语) 算法不同, 真正互补.
  - sentiment:   基于内置情感词典的极性 + 得分 [-1, 1], 含否定/避免上下文处理
  - summary:     按字数截取的 1-2 句摘要
  - heat_score:  内部热度 = log1p(liked*1 + collected*3 + comment*2 + share*4) * 时效因子
  - is_short:    desc 字数 < 40 的标记
  - is_ad_like:  简单启发式 (过多 # / @ / 外部链接)

评论条目附加:
  - sentiment / sentiment_score
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import jieba
    import jieba.analyse
    JIEBA_OK = True
except Exception:
    JIEBA_OK = False

LOG = logging.getLogger("enrich")


_POSITIVE = {
    "喜欢", "推荐", "好用", "好看", "惊艳", "宝藏", "完美", "满意", "惊喜", "舒服",
    "棒", "赞", "爱", "值得", "强烈", "加分", "高级", "绝美", "绝绝子",
    "治愈", "方便", "实用", "贴心", "用心", "质感", "氛围", "心动", "颜值",
    "便宜", "划算", "物美价廉", "回购", "种草", "必入", "入手", "力推", "超强", "高分",
    "好吃", "好喝", "香", "鲜", "甜", "幸福", "开心", "快乐",
}
_NEGATIVE = {
    "难用", "吐槽", "踩雷", "失望", "后悔", "不好", "差", "丑", "难吃", "难喝",
    "贵", "不值", "廉价", "假", "骗子", "退款", "退货", "刺激", "过敏",
    "智商税", "避雷", "劝退", "翻车", "翻车现场", "亏", "硬伤", "坑", "套路", "敷衍",
    "粗糙", "塑料", "穿帮", "塑料感", "坏掉", "碎", "卡", "崩溃", "无聊",
    "噪音", "吵", "闷", "压抑", "难闻", "脏", "油腻", "粘", "卡粉", "脱妆",
}
_STOPWORDS = {
    "的", "了", "和", "是", "就", "都", "而", "及", "与", "或",
    "一个", "没有", "我们", "你们", "他们", "她们", "以及", "因为", "所以", "但是",
    "如果", "虽然", "然后", "因此", "于是", "而且", "并且", "这样", "那样", "这个",
    "那个", "这里", "那里", "这么", "那么", "什么", "怎么", "为什么", "怎样", "可以",
    "不能", "不要", "应该", "已经", "正在", "现在", "以前", "以后", "今天", "明天",
    "https", "http", "com", "www", "小红书", "分享", "日常", "记录", "笔记",
    "生活", "真实", "原创", "打卡", "安利", "来啦", "来咯", "啦",
    "呀", "哦", "呢", "啊", "嗯", "哈", "哈哈", "嘻嘻", "哦哦",
    "xhs", "xhsapp", "xhsweb", "app", "web", "pc", "网页", "链接",
}

# 互联网 / 校招 / 职场话题扩展词 (2025-2026)
_NEGATIVE.update({
    # 招聘 / 就业环境
    "hc缩减", "HC缩减", "缩招", "砍hc", "hc砍", "hc缩水", "hc大缩水",
    "砍hc", "HC砍", "HC缩", "招满", "没hc", "没有hc", "hc不够",
    "池子", "泡池子", "泡两个月", "排序中", "排序", "在排序",
    "凉", "凉了", "凉了凉", "凉经", "挂", "挂了", "挂经", "一面挂", "三面挂",
    "卷", "卷到飞起", "卷麻了", "卷不动", "卷生卷死", "卷成这样",
    "寒冬", "互联网寒冬", "大寒冬", "下半场", "过冬",
    "焦虑", "心态崩", "心态炸", "崩了", "炸了", "麻", "麻了",
    "劝退", "劝退指南", "放弃", "没戏", "没机会", "没希望",
    "失败", "凉了", "全军覆没", "泡池", "石沉大海", "无回音",
    "竞争", "卷到", "压力", "焦虑感", "失眠", "想躺",
    "难顶", "顶不住", "受不了", "顶不住", "降薪", "裁员",
    "35岁危机", "中年危机", "被动离职", "被裁", "被优化",
    "996", "007", "加班", "内卷", "卷王", "卷出新高度",
    # 求职 / 学历
    "门槛高", "门槛也高", "门槛也卷", "卷到飞起", "卷上天",
    "学历卷", "学校差", "双非", "非92", "非985",
    # 待遇
    "降薪", "降级", "白菜价", "白菜", "低于预期", "sp毁约",
    "实习失败", "转正失败", "答辩没过",
})
_POSITIVE.update({
    # 求职 / offer
    "上岸", "拿到offer", "拿下offer", "get offer", "get了offer",
    "offer到手", "成功上岸", "成功拿offer", "顺利", "顺利上岸",
    "扩招", "逆势扩招", "hc充足", "hc相对充足", "hc还行",
    "回暖", "回暖迹象", "招聘回暖", "回暖中",
    "机会多", "hc多", "hc还多", "hc相对多",
    "上岸了", "拿到心仪", "心仪的offer", "dream offer", "dream offer到手",
    "保研", "保研成功", "推免", "推免成功",
    # 行业 / 公司
    "香", "很香", "真香", "香饽饽", "方向好", "有前景",
    "蓝海", "风口", "扩招", "扩招hc", "开hc",
    "大厂", "大厂offer", "进大厂", "去了大厂",
    # 工作相关
    "加班少", "WLB", "work life balance", "wlb", "955", "1075",
    "氛围好", "同事好", "leader好", "技术好", "成长快",
    "转正通过", "转正答辩通过", "答辩通过", "拿到转正",
    # 评价
    "爱了", "感恩", "感谢", "太棒了", "必入", "力推", "yyds",
    "便宜", "性价比", "划算", "高于预期", "sp到手", "ssp",
})



def extract_keywords(text, topk=10):
    if not text:
        return []
    if not JIEBA_OK:
        return _fallback_keywords(text, topk)
    try:
        tags = jieba.analyse.extract_tags(text, topK=topk, withWeight=False)
        return [t for t in tags if t and t not in _STOPWORDS and len(t) >= 2][:topk]
    except Exception as exc:
        LOG.warning("jieba.extract_tags 失败: %s", exc)
        return _fallback_keywords(text, topk)


def _fallback_keywords(text, topk):
    """无 jieba 时退化: 切出 2-gram + 词频。"""
    if not text:
        return []
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z]+", "", text)
    grams = []
    for i in range(len(text) - 1):
        if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i + 1] <= "\u9fff":
            grams.append(text[i:i + 2])
    freq = {}
    for g in grams:
        freq[g] = freq.get(g, 0) + 1
    sorted_grams = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    out = []
    for g, _ in sorted_grams:
        if g in _STOPWORDS:
            continue
        out.append(g)
        if len(out) >= topk:
            break
    return out


_POS_RE = re.compile("|".join(sorted(_POSITIVE, key=len, reverse=True)))
_NEG_RE = re.compile("|".join(sorted(_NEGATIVE, key=len, reverse=True)))

# 否定/避免前缀 — 出现这些词后的正/负词不计入该极性
# (例如 "避免烂脸" / "不刺激" / "防止翻车" / "拒绝内卷" 应理解为正面建议, 不是负面)
_NEGATION_BEFORE = ("不", "别", "避免", "防止", "无需", "没有", "不易", "不会",
                    "不要", "绝不", "杜绝", "难以", "不是", "不怎么", "不算",
                    "拒绝", "反对", "不曾", "未曾")
_NEG_WINDOW = 8  # 词前 8 字窗口内出现否定前缀就跳过


def _count_with_negation(text: str, words_re: re.Pattern) -> int:
    """对每个匹配位置, 检查前 8 字窗口是否含否定/避免前缀. 含则跳过."""
    count = 0
    for m in words_re.finditer(text):
        pre = text[max(0, m.start() - _NEG_WINDOW):m.start()]
        if any(neg in pre for neg in _NEGATION_BEFORE):
            continue
        count += 1
    return count


def sentiment_score(text):
    if not text:
        return 0.0
    pos = _count_with_negation(text, _POS_RE)
    neg = _count_with_negation(text, _NEG_RE)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


def sentiment_label(score):
    if score >= 0.34:
        return "positive"
    if score <= -0.34:
        return "negative"
    return "neutral"


_SENT_SPLIT = re.compile(r"(?<=[。!?\n])")


def make_summary(text, max_chars=140):
    if not text:
        return ""
    parts = [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]
    if not parts:
        return text[:max_chars]
    out = []
    total = 0
    for p in parts:
        if total + len(p) > max_chars:
            break
        out.append(p)
        total += len(p)
    return " ".join(out) if out else parts[0][:max_chars]


def heat_score(interact, ts_ms, now_ms=None):
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    liked = interact.get("liked") or 0
    collected = interact.get("collected") or 0
    comment = interact.get("comment") or 0
    share = interact.get("share") or 0
    base = math.log1p(max(0, liked) * 1 + max(0, collected) * 3 + max(0, comment) * 2 + max(0, share) * 4)
    if not ts_ms:
        recency = 0.5
    else:
        age_days = max(0, (now_ms - ts_ms) / (1000 * 86400))
        recency = 1 / (1 + age_days / 7)
    return round(base * (0.5 + 0.5 * recency), 4)


def ad_like_score(text, tags):
    if not text:
        return 0.0
    n_hash = text.count("#")
    n_at = text.count("@")
    n_url = len(re.findall(r"https?://", text))
    n_tag = len(tags or [])
    score = 0
    score += min(3, n_hash) * 0.05
    score += min(2, n_at) * 0.08
    score += min(2, n_url) * 0.25
    if n_tag >= 6:
        score += 0.15
    return round(min(1.0, score), 4)


def extract_topics(text: str, top_k: int = 5) -> list:
    """用 jieba.analyse.textrank 抽取关键短语 (与 extract_tags 单 TF-IDF 不同).

    历史: 旧版 'topics' 字段是 normalize_topics(tags), 实际就是 tags 去空格版,
    agent 看 report.md 困惑 (所谓 '主题' 跟 tags 完全一样). 改用 textrank:
      - keywords: TF-IDF 单高频词 (jieba.analyse.extract_tags)
      - topics:   TextRank 关键短语 (jieba.analyse.textrank, graph-based)
    两算法侧重不同: keywords 偏统计频率, topics 偏语义中心. desc 空时
    返回 [] (与之前 normalize_topics 对 tags=[] 行为一致).
    """
    if not text:
        return []
    try:
        return jieba.analyse.textrank(text, topK=top_k)
    except Exception:
        return []


def normalize_topics(tags):
    """deprecated: 旧版 topics 实现 = tags 去空格. 现在用 extract_topics."""
    if not tags:
        return []
    out = []
    seen = set()
    for t in tags:
        if not t:
            continue
        nt = t.replace(" ", "").strip()
        if not nt or nt in seen:
            continue
        seen.add(nt)
        out.append(nt)
    return out


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


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def enrich_note(rec):
    desc = rec.get("desc_plain") or ""
    title = rec.get("title") or ""
    tags = rec.get("tags") or []
    text_for_kw = (title + "\n" + desc).strip()
    keywords = extract_keywords(text_for_kw, topk=10)
    topics = extract_topics(text_for_kw, top_k=5)
    sentiment = sentiment_score(text_for_kw)
    summary = make_summary(desc)
    interact = rec.get("interact") or {}
    ts_ms = rec.get("ts") or 0
    heat = heat_score(interact, ts_ms)
    ad_like = ad_like_score(rec.get("desc") or desc, tags)
    word_count = rec.get("word_count") or 0

    rec["keywords"] = keywords
    rec["topics"] = topics
    rec["sentiment"] = sentiment_label(sentiment)
    rec["sentiment_score"] = sentiment
    rec["summary"] = summary
    rec["heat_score"] = heat
    rec["ad_like_score"] = ad_like
    rec["is_short"] = bool(word_count < 40)
    rec["enriched_at"] = datetime.now(timezone.utc).isoformat()
    return rec


def enrich_comment(rec):
    """为评论 / 子评论做增强: 关键词, 情感, 摘要, 热度。"""
    content = rec.get("content") or rec.get("comment_content") or ""
    if content:
        rec["keywords"] = extract_keywords(content, topk=8)
        s = sentiment_score(content)
        rec["sentiment"] = sentiment_label(s)
        rec["sentiment_score"] = s
        rec["summary"] = make_summary(content)
        rec["heat_score"] = round(0.5 * rec.get("liked", 0), 4)
        rec["ad_like_score"] = ad_like_score(content, [])
        rec["is_short"] = bool(rec.get("word_count", 0) < 15)
    rec["enriched_at"] = datetime.now(timezone.utc).isoformat()
    return rec

def enrich_records(records):
    enriched = []
    for rec in records:
        if rec.get("is_comment"):
            rec = enrich_comment(rec)
        elif rec.get("is_comment") is False:
            rec = enrich_note(rec)
        enriched.append(rec)
    return enriched

def main(argv=None):
    p = argparse.ArgumentParser(description="小红书数据增强")
    p.add_argument("--in", dest="src", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    if not JIEBA_OK:
        LOG.warning("jieba 未安装, 使用 n-gram 回退方案")

    records = _read_jsonl(Path(args.src))
    LOG.info("读入 %d 条清洗记录", len(records))
    enriched = enrich_records(records)
    n = _write_jsonl(Path(args.out), enriched)
    LOG.info("输出 %d 条增强记录 -> %s", n, args.out)
    print(f"已写入 {args.out}: {n} 条 (输入 {len(records)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
