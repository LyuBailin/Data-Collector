# -*- coding: utf-8 -*-
"""tests/test_login_wall.py — 锁住 playwright_driver._detect_login_wall 契约.

为什么这个测试存在:
  之前 page_search_notes / page_user_posted / page_hotlist 的
  "未返回任何结果" 错误一律报 '可能触发风控'. 实测 cookie 过期时
  XHS 渲染登录墙 ('登录后查看搜索结果' + QR 码), 根本不发
  /v2/search/notes. 错误信息把'重新导出 cookie' 的问题误报为
  '等几分钟重试', agent 走错修复路径.

  本测试钉死 _detect_login_wall 的检测语义, 防止以后回归.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from playwright_driver import _detect_login_wall  # noqa: E402


class _MockPage:
    """最小 mock page, 支持 await page.title() 和 await page.evaluate(js)."""

    def __init__(self, title: str, body: str):
        self._title = title
        self._body = body

    async def title(self) -> str:
        return self._title

    async def evaluate(self, js: str) -> Any:
        # js 形如 "document.body ? document.body.innerText : ''"
        # 直接返回 self._body 即可
        return self._body


def run(coro):
    """asyncio.run 的薄封装, 让测试同步写."""
    return asyncio.run(coro)


class DetectLoginWall(unittest.TestCase):
    """_detect_login_wall 必须按字面 marker 命中."""

    def test_login_wall_marker_in_body(self):
        page = _MockPage(
            title="本周热榜 - 小红书搜索",
            body="登录后查看搜索结果\n可用\n小红书\n扫码\n手机号登录\n登录",
        )
        self.assertTrue(run(_detect_login_wall(page)))

    def test_qr_login_in_body(self):
        page = _MockPage(title="xhs", body="扫码登录\n小红书如何扫码")
        self.assertTrue(run(_detect_login_wall(page)))

    def test_normal_search_results_no_false_positive(self):
        # 正常搜索结果页不应该触发登录墙检测.
        # XHS nav-bar 始终有 '登录' 按钮 (未登录态), 不能误报.
        page = _MockPage(
            title="本周热榜 - 小红书搜索",
            body="发现 直播 发布 通知 消息 登录 关注",
        )
        self.assertFalse(run(_detect_login_wall(page)))

    def test_empty_body_returns_false(self):
        page = _MockPage(title="", body="")
        self.assertFalse(run(_detect_login_wall(page)))

    def test_only_title_login_wall_marker(self):
        # 标题里有 '请先登录' 也算登录墙
        page = _MockPage(title="请先登录", body="")
        self.assertTrue(run(_detect_login_wall(page)))

    def test_page_evaluate_failure_returns_false(self):
        # page.evaluate 抛异常时不应崩, 应返回 False (graceful degradation)
        class _BrokenPage(_MockPage):
            async def evaluate(self, js):
                raise RuntimeError("page closed")

        page = _BrokenPage("登录", "登录后查看")
        self.assertFalse(run(_detect_login_wall(page)))


class LoginWallMarkersContract(unittest.TestCase):
    """marker 列表不能少 (新 marker 加入时同步改测试).

    这些是实测出现的 XHS 登录墙文案, 任何一个被改 / 删都会让 cookie
    过期检测失灵, agent 又会拿到 '可能触发风控' 走错修复路径.
    """

    def test_required_markers_present(self):
        from playwright_driver import _LOGIN_WALL_MARKERS
        required = [
            "登录后查看",        # 搜索结果页核心文案
            "扫码登录",          # QR 登录页
            "手机号登录",        # 手机号登录页
            "请先登录",          # 通用拦截文案
            "未登录",            # 部分页面的 '未登录' 提示
        ]
        for r in required:
            self.assertIn(
                r, _LOGIN_WALL_MARKERS,
                f"marker {r!r} 缺失, cookie 过期会被误报为风控",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)