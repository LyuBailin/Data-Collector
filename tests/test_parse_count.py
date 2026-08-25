# -*- coding: utf-8 -*-
"""tests/test_parse_count.py — 锁住 collect._parse_count 契约.

为什么这个测试存在:
  2026-08-25 真实 cookie 跑 pipeline --keywords 时,
  WARNING '补全 ... 失败: invalid literal for int() with base 10: '1.3万''
  让一条笔记整条跳过. XHS 在大数字 (>= 10000) 上把互动字段返回成
  '1.3万' / '1.2w' 这种带单位的字符串, int() 直接 ValueError.

  本测试钉死 _parse_count 对所有已知格式的解析:
    中文万 (1.3万) / Western w (1.2w) / k (5.4k) / + 后缀 (1000+)
    / 千分位 (1,234) / int / float / None / 空串 / 完全乱码
  任一回归立即红.

跑法 (stdlib):
  python tests/test_parse_count.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from collect import _parse_count  # noqa: E402


class ParseCountHappyPath(unittest.TestCase):
    """正常数字 / 字符串数字应正确解析."""

    def test_int(self):
        self.assertEqual(_parse_count(0), 0)
        self.assertEqual(_parse_count(42), 42)
        self.assertEqual(_parse_count(1000000), 1000000)

    def test_float_truncates(self):
        # XHS 偶尔返回 float
        self.assertEqual(_parse_count(3.0), 3)
        self.assertEqual(_parse_count(100.7), 100)

    def test_plain_digit_string(self):
        self.assertEqual(_parse_count("0"), 0)
        self.assertEqual(_parse_count("1234"), 1234)

    def test_thousands_separator(self):
        self.assertEqual(_parse_count("1,234"), 1234)
        self.assertEqual(_parse_count("1,234,567"), 1234567)


class ParseCountChineseUnit(unittest.TestCase):
    """中文 '万' 单位 -> 10000."""

    def test_basic_wan(self):
        self.assertEqual(_parse_count("1万"), 10000)

    def test_decimal_wan(self):
        self.assertEqual(_parse_count("1.3万"), 13000)

    def test_small_decimal_wan(self):
        self.assertEqual(_parse_count("0.5万"), 5000)


class ParseCountWesternUnit(unittest.TestCase):
    """Western w/k 单位 -> 10000/1000."""

    def test_lowercase_w(self):
        self.assertEqual(_parse_count("1.2w"), 12000)
        self.assertEqual(_parse_count("2w"), 20000)

    def test_uppercase_W(self):
        # XHS 也偶尔用大写
        self.assertEqual(_parse_count("1.2W"), 12000)

    def test_lowercase_k(self):
        self.assertEqual(_parse_count("5.4k"), 5400)
        self.assertEqual(_parse_count("100k"), 100000)


class ParseCountPlusSuffix(unittest.TestCase):
    """'1000+' 后缀 (XHS 表示 '至少 1000') 应去掉 + 解析数字."""

    def test_plus(self):
        self.assertEqual(_parse_count("1000+"), 1000)
        self.assertEqual(_parse_count("1万+"), 10000)


class ParseCountEdgeCases(unittest.TestCase):
    """None / 空串 / 乱码 -> 0 (graceful, 不抛)."""

    def test_none(self):
        self.assertEqual(_parse_count(None), 0)

    def test_empty_string(self):
        self.assertEqual(_parse_count(""), 0)

    def test_whitespace_only(self):
        self.assertEqual(_parse_count("   "), 0)

    def test_garbage_string(self):
        # 完全无法解析 -> 0
        self.assertEqual(_parse_count("abc"), 0)
        self.assertEqual(_parse_count("--"), 0)

    def test_bool_rejected(self):
        # bool 是 int 子类 (True=1, False=0), 但语义上不应用作 count
        self.assertEqual(_parse_count(True), 0)
        self.assertEqual(_parse_count(False), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)