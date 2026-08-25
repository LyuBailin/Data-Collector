# -*- coding: utf-8 -*-
"""tests/test_slugify.py — 锁住 pipeline._slugify 的大小写保留契约.

为什么这个测试存在:
  commit 5d33c40 后的实测发现: --keywords 'AI神器' 通过 _slugify 转 'ai神器'
  生成 folder '2026-08-25_ai神器', 但 --runs 'AI神器' 拼写不一致导致
  cross_analyze 匹配失败. 改造为不 lowercase 后, slug 与 keyword 字面一致,
  让 --keywords / --runs / --dimensions 三处 CLI 参数可同步使用同一字符串.

跑测试 (stdlib):
  python tests/test_slugify.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from pipeline import _slugify  # noqa: E402


class SlugifyCasePreservation(unittest.TestCase):
    """ASCII 大小写必须保留 (与 keyword 字面一致)."""

    def test_uppercase_kept(self):
        self.assertEqual(_slugify("AI"), "AI")

    def test_mixed_case_kept(self):
        self.assertEqual(_slugify("AI神器"), "AI神器")
        self.assertEqual(_slugify("GPT-4o"), "GPT-4o")

    def test_lowercase_unchanged(self):
        self.assertEqual(_slugify("ai"), "ai")
        self.assertEqual(_slugify("ai神器"), "ai神器")


class SlugifyUnicodePreservation(unittest.TestCase):
    """中文 / 数字 / 符号处理."""

    def test_chinese_kept(self):
        self.assertEqual(_slugify("秋招"), "秋招")
        self.assertEqual(_slugify("小红书"), "小红书")

    def test_digits_kept(self):
        self.assertEqual(_slugify("hc 2026"), "hc_2026")

    def test_space_to_underscore(self):
        self.assertEqual(_slugify("183 胖穿搭"), "183_胖穿搭")

    def test_hyphen_kept(self):
        self.assertEqual(_slugify("AI-tools"), "AI-tools")

    def test_special_chars_dropped(self):
        # emoji / punctuation 应被丢弃
        self.assertEqual(_slugify("AI?!神器"), "AI神器")


class SlugifyLengthLimit(unittest.TestCase):
    """60 字符截断."""

    def test_truncates_long_input(self):
        long_input = "a" * 100
        self.assertEqual(len(_slugify(long_input)), 60)

    def test_empty_returns_default(self):
        self.assertEqual(_slugify(""), "default")


if __name__ == "__main__":
    unittest.main(verbosity=2)