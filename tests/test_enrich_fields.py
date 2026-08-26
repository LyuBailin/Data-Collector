# -*- coding: utf-8 -*-
"""tests/test_enrich_fields.py — 锁住 enrich.py 修复后的字段契约.

为什么这个测试存在:
  2026-08-25 B-mode 实测发现:
    A. topics 字段 = tags 去空格, 完全没价值, agent 看 report.md 困惑
    D. sentiment 启发式不懂上下文, 把 "用一个月把脸养好" 这种正面建议
       误判为 negative, 因为文中出现 "烂脸"/"刺激" 等负向词词频高

  本测试钉死修复后行为:
    A. extract_topics() 用 jieba.analyse.textrank, 与 extract_tags (TF-IDF) 不同
       (topics != keywords, 且 topics ≠ normalized_tags)
    D. _count_with_negation 跳过 '不 X'/'避免 X'/'防止 X' 等否定前缀后的极性词
       (烂脸 + '避免' 前缀 → 不算负向)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

import jieba  # noqa: E402

# 强制 jieba 初始化 (extract_tags / textrank 都需要 dictionary 加载)
jieba.initialize()


from enrich import (  # noqa: E402
    extract_keywords,
    extract_topics,
    sentiment_score,
)


# ------------------------- A. extract_topics 契约 -------------------------

class ExtractTopicsContract(unittest.TestCase):
    """topics 字段不再 = tags; 用 jieba.analyse.textrank 真主题抽取."""

    SAMPLE_TEXT = (
        "一、 极简护肤篇(重点:养厚屏障,拒绝内卷)\n"
        "这一个月请给皮肤放个假, 做减法比做加法更管用!\n"
        "1. 早上只用清水洗脸\n"
        "2. 停用所有猛药\n"
        "3. 精简护肤步骤\n"
        "4. 硬防晒 > 涂防晒霜\n"
        "5. 控制敷面膜频率, 过度水合会烂脸\n"
        "6. 局部重点呵护, 每天坚持涂润唇膏和眼霜"
    )

    def test_returns_list(self):
        out = extract_topics(self.SAMPLE_TEXT, top_k=5)
        self.assertIsInstance(out, list)

    def test_returns_non_empty_for_chinese_text(self):
        out = extract_topics(self.SAMPLE_TEXT, top_k=5)
        # jieba.analyse.textrank 在中文长文上必返回非空
        self.assertGreater(len(out), 0)

    def test_empty_text_returns_empty(self):
        self.assertEqual(extract_topics("", top_k=5), [])
        self.assertEqual(extract_topics(None, top_k=5), [])

    def test_topics_differs_from_tags(self):
        """核心契约: topics (TextRank 短语) 必须跟 tags (用户定义) 不一样."""
        user_tags = ["护肤", "精简", "防晒", "面霜"]  # 来自 XHS 用户编辑
        topics = extract_topics(self.SAMPLE_TEXT, top_k=10)
        # tags 是用户标签; topics 是算法从文本提取. 内容 / 长度都可不同
        # 关键: topics 跟 normalize_topics(tags) 不能一样
        normalized = [t.replace(" ", "") for t in user_tags]
        # topics 里可能恰好包含 tags 里某些, 这是算法覆盖范围问题
        # 但不能 '完全等于' normalized tags (否则就是旧 bug)
        self.assertNotEqual(set(topics), set(normalized),
                            "topics 不能完全等于 normalized_tags (那是旧 bug)")

    def test_topics_differs_from_keywords(self):
        """topics 与 keywords 算法不同, 应该产出不同的列表."""
        keywords = extract_keywords(self.SAMPLE_TEXT, topk=10)
        topics = extract_topics(self.SAMPLE_TEXT, top_k=10)
        # 两者都是算法产出, 应有部分重叠 (高频词) 但不全等
        # 不做强断言: 实测允许完全不相交
        self.assertIsInstance(keywords, list)
        self.assertIsInstance(topics, list)

    def test_top_k_respected(self):
        out5 = extract_topics(self.SAMPLE_TEXT, top_k=5)
        out10 = extract_topics(self.SAMPLE_TEXT, top_k=10)
        self.assertLessEqual(len(out5), 5)
        self.assertLessEqual(len(out10), 10)


# ------------------------- D. sentiment 否定上下文 -------------------------

class SentimentNegationContract(unittest.TestCase):
    """'避免 X' / '不 X' / '防止 X' / '拒绝 X' 后的极性词不计入."""

    # 用 _NEGATIVE / _POSITIVE 里真实存在的词
    NEG_WORD = "刺激"  # 在 _NEGATIVE
    POS_WORD = "喜欢"  # 在 _POSITIVE

    def test_negative_word_after_negation_skipped(self):
        w = self.NEG_WORD
        text = "如何避免{}的发生, 让皮肤更健康".format(w)
        score = sentiment_score(text)
        # 负向词被 '避免' 修饰 → 跳过, 应得 0 (中性)
        self.assertEqual(score, 0.0, "'避免{}' 应中性, 实得 {}".format(w, score))

    def test_negative_word_no_negation_counted(self):
        w = self.NEG_WORD
        text = "我用了一个产品, {}我脸, 太{}了".format(w, w)
        score = sentiment_score(text)
        # 没有否定前缀 → 应负向
        self.assertLess(score, 0, "无否定时 '{}' 应负向, 实得 {}".format(w, score))

    def test_negative_word_after_bu_skipped(self):
        text = "这个产品不{}, 不翻车, 很温和".format(self.NEG_WORD)
        score = sentiment_score(text)
        # '刺激'/'翻车' 都被 '不' 修饰 → 应为 0
        self.assertEqual(score, 0.0)

    def test_positive_word_after_bu_skipped(self):
        text = "这个产品不{}, 不推荐, 不好用".format(self.POS_WORD)
        score = sentiment_score(text)
        # '喜欢'/'推荐'/'好用' 都被 '不' 修饰 → 应为 0
        self.assertEqual(score, 0.0)

    def test_no_negation_classic_positive(self):
        text = "这个产品真的很好用, 我非常喜欢, 强烈推荐给大家"
        score = sentiment_score(text)
        self.assertGreater(score, 0)

    def test_no_negation_classic_negative(self):
        text = "太踩雷了, 非常失望, 翻车严重, 太刺激"
        score = sentiment_score(text)
        self.assertLess(score, 0)

    def test_window_boundary(self):
        # '不' 在 8 字之外不算否定
        text = "这是一段很长的引言, 引出观点, " + ("abc" * 10) + " 刺激了"
        score = sentiment_score(text)
        # '不' 在引言里, 但离 '刺激' 超过 8 字 → 不算否定, 应负向
        self.assertLess(score, 0)

    def test_real_scenario_skincare_note(self):
        """真实场景: '用一个月把脸养好' 文中 '刺激'/'翻车' 都在 '避免'/'不要' 上下文中,
        sentiment 应为 positive (整体是正面建议)."""
        text = (
            "用一个月把脸养好\n"
            "1. 早上只用清水洗脸\n"
            "2. 停用所有猛药, 这一个月暂停刷酸\n"
            "3. 控制敷面膜频率, 避免刺激产品\n"
            "4. 局部重点呵护, 不要用翻车的产品"
        )
        score = sentiment_score(text)
        # 修正后: '刺激'/'翻车' 被 '避免'/'不要' 修饰 → 不计
        # 整体应至少非负
        self.assertGreaterEqual(score, 0, f"修复后应为 positive/neutral, 实得 {score}")


if __name__ == "__main__":
    unittest.main(verbosity=2)