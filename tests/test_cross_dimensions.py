# -*- coding: utf-8 -*-
"""tests/test_cross_dimensions.py — 锁住 cross_analyze.py --dimensions 格式契约。

为什么这个测试存在:
  之前的 SKILL.md §4.2 第 139 行用过时格式 "hc,interview,company",
  现在的 parse_dimensions 见到旧格式直接 exit 3, agent 按 SKILL 抄过去
  就跑挂. 本测试把以下契约钉死, 任何回退立即失败:

  1. parse_dimensions 只接受 "name:kw1,kw2;name2:kw3,kw4" 新格式
  2. 缺冒号 / 空维度名 / 空关键词 / 维度名重复 / 整段为空 → ValueError
  3. 解析结果是 {name: frozenset/keywords} (维度名到关键词集合)
  4. CLI 子进程调用, 错误格式应 exit 3 (参数错误), 正确格式能进聚合阶段

跑法 (stdlib, 不需要 pytest):
  cd <repo-root>
  python tests/test_cross_dimensions.py            # 跑全部
  python tests/test_cross_dimensions.py -v         # 详细输出
  python -m unittest tests.test_cross_dimensions   # 也可

如果脚本改了 parse_dimensions 的语义, 这个测试就会失败提醒 PR 作者同步更新
SKILL.md / agents/openai.yaml 里的 --dimensions 例子.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"

# 让 from cross_analyze import parse_dimensions 能跑
sys.path.insert(0, str(SCRIPTS))

from cross_analyze import parse_dimensions  # noqa: E402


class ParseDimensionsHappyPath(unittest.TestCase):
    """正常输入解析正确."""

    def test_single_dimension(self):
        out = parse_dimensions("fit:穿搭,显瘦,搭配")
        self.assertEqual(out, {"fit": {"穿搭", "显瘦", "搭配"}})

    def test_multiple_dimensions(self):
        out = parse_dimensions(
            "fit:穿搭,搭配,显瘦;brand:大码,微胖,品牌"
        )
        self.assertEqual(
            out,
            {"fit": {"穿搭", "搭配", "显瘦"}, "brand": {"大码", "微胖", "品牌"}},
        )

    def test_whitespace_stripped(self):
        out = parse_dimensions(" fit : 穿搭 , 搭配 ; brand : 大码 , 品牌 ")
        self.assertEqual(
            out,
            {"fit": {"穿搭", "搭配"}, "brand": {"大码", "品牌"}},
        )

    def test_chinese_keywords_preserved(self):
        # 中文关键词必须原样保留, 不能 strip 掉也不能 unicode-normalize 错位
        out = parse_dimensions("hc:hc,缩招,秋招,校招,暑期转正")
        self.assertEqual(
            out,
            {"hc": {"hc", "缩招", "秋招", "校招", "暑期转正"}},
        )

    def test_empty_segments_between_semicolons_skipped(self):
        # ';;' 中间空段应被跳过, 而不是报错
        out = parse_dimensions("a:k1,k2;;b:k3")
        self.assertEqual(out, {"a": {"k1", "k2"}, "b": {"k3"}})


class ParseDimensionsFailLoud(unittest.TestCase):
    """错误格式必须抛 ValueError (CLI 层会转 exit 3), 不能静默跳过.

    这些是契约: agent 拼错时, 应该立刻知道拼错, 而不是聚合出来一份看似正常但
    维度全错的报告.
    """

    def test_missing_colon_rejected(self):
        # 旧格式 "hc,interview,company" 必须挂, 这是本测试存在的核心目的.
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions("hc,interview,company")
        self.assertIn("缺冒号", str(ctx.exception))

    def test_empty_dimension_name_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions(":kw1,kw2")
        self.assertIn("维度名为空", str(ctx.exception))

    def test_empty_keywords_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions("fit:")
        self.assertIn("关键词为空", str(ctx.exception))

    def test_empty_keywords_all_blank_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions("fit: , , ")
        self.assertIn("关键词为空", str(ctx.exception))

    def test_duplicate_dimension_name_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions("fit:k1;fit:k2")
        self.assertIn("维度名重复", str(ctx.exception))

    def test_empty_string_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_dimensions("")
        self.assertIn("--dimensions 为空", str(ctx.exception))

    def test_only_semicolons_rejected(self):
        # 全是分号没有维度段
        with self.assertRaises(ValueError):
            parse_dimensions(";;;")


class CliSubprocessIntegration(unittest.TestCase):
    """子进程调用验证: 错误格式 exit 3, 正确格式能进聚合阶段.

    用真实 workspace data/runs/sample (已在仓库里, 是 mock 数据) 跑;
    因为 sample 不含 'testkw' 关键词, 正常格式会 exit 2 (稀薄) 而不是 0 —
    这里不验证聚合内容, 只验证: 错误格式 → exit 3, 正确格式 → exit != 3.
    """

    def _run_cli(self, dimensions_arg):
        # 用临时文件作为 --output, 避免污染 tests/ 目录
        tmp_out = tempfile.NamedTemporaryFile(
            prefix="cross_test_", suffix=".json", delete=False
        )
        tmp_out.close()
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "cross_analyze.py"),
                "--runs", "sample",
                "--dimensions", dimensions_arg,
                "--workspace", str(REPO_ROOT / "data" / "runs"),
                "--output", tmp_out.name,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_old_format_exits_3(self):
        """保护: SKILL.md §4.2 的旧格式 'hc,interview,company' 跑挂 = 测试通过"""
        result = self._run_cli("hc,interview,company")
        self.assertEqual(
            result.returncode, 3,
            f"旧格式应 exit 3, 实得 {result.returncode}. stderr={result.stderr}",
        )
        self.assertIn("缺冒号", result.stderr)

    def test_new_format_does_not_exit_3(self):
        """新格式不能 exit 3 (格式错). exit 0/2 都 OK (0=有命中, 2=稀薄)"""
        result = self._run_cli("fit:穿搭,搭配;brand:大码,品牌")
        self.assertNotEqual(
            result.returncode, 3,
            f"新格式不应 exit 3, 实得 {result.returncode}. stderr={result.stderr}",
        )


class RunFolderSelectionContract(unittest.TestCase):
    """_load_run 必须对 agent 可见 (WARNING + 选中哪个).

    这条契约防止 agent 报告引用的 run 已被覆盖而自己不知道.
    本测试用临时目录验证:
      - 多候选 → WARNING 列出所有 + 选中哪个
      - 无候选 → WARNING
    """

    def setUp(self):
        # sys.path 已经包含 SCRIPTS
        import cross_analyze
        self.cross_analyze = cross_analyze
        # 用临时 workspace 避免污染真实 data/runs
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_workspace = Path(self._tmp.name)
        # 创建 3 个候选 folder (slug=foo), 期望选中 _2
        for n in [None, "_1", "_2"]:
            d = self.tmp_workspace / f"2026-08-25_foo{n or ''}"
            d.mkdir()
            (d / "enriched.jsonl").write_text(
                json.dumps({"note_id": f"id_{n or 'base'}"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def tearDown(self):
        self._tmp.cleanup()

    def test_multi_candidate_logs_warning_with_choice(self):
        import logging
        captured = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        log = logging.getLogger("cross_analyze")
        log.addHandler(_CaptureHandler())
        log.setLevel(logging.WARNING)
        try:
            records = self.cross_analyze._load_run("foo", self.tmp_workspace)
        finally:
            log.removeHandler(_CaptureHandler())
        # 应该至少 1 条 WARNING, 且提到 _2
        warnings = [m for m in captured if "WARNING" not in m and "foo" in m]
        self.assertTrue(len(warnings) >= 1, f"应有 WARNING, 实得: {captured}")
        self.assertTrue(
            any("_2" in m for m in captured),
            f"WARNING 应提及选中的 _2, 实得: {captured}",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["note_id"], "id__2")

    def test_no_candidate_logs_warning(self):
        import logging
        captured = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        log = logging.getLogger("cross_analyze")
        log.addHandler(_CaptureHandler())
        log.setLevel(logging.WARNING)
        try:
            records = self.cross_analyze._load_run("does_not_exist", self.tmp_workspace)
        finally:
            log.removeHandler(_CaptureHandler())
        self.assertEqual(records, [])
        self.assertTrue(
            any("无匹配 run folder" in m for m in captured),
            f"应有 '无匹配' WARNING, 实得: {captured}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)