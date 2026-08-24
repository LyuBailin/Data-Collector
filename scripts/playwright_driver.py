# -*- coding: utf-8 -*-
"""
playwright_driver.py — 在 Chromium 浏览器里直接发 XHS 请求

思路:
  XHS 的 X-s / X-t 签名依赖一连串浏览器内私有变量 (window.mnsv2,
  webpack closure 里的 buildEncSskSign 等), 在 node.js 沙箱里
  复现非常痛苦。

  干脆让浏览器自己处理签名: 启动 Chromium, 注入 cookie,
  加载 xiaohongshu.com 触发所有脚本 (包括动态 SDK),
  然后 page.evaluate(fetch(url, ...)) 让浏览器发请求,
  浏览器自己的 axios 拦截器会加 X-s / X-t。

CLI:
  python scripts/playwright_driver.py --probe
  python scripts/playwright_driver.py --request '{"url":..., "method":..., "headers":..., "body":...}'

输出: JSON {status, headers, body} 写到 stdout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from playwright.async_api import async_playwright, BrowserContext, Page
except ImportError:
    print("需要安装 playwright: pip install playwright && playwright install chromium", file=sys.stderr)
    raise

LOG = logging.getLogger("playwright_driver")

COOKIES_FILE = "assets/cookies.json"
DEFAULT_BASE_URL = "https://www.xiaohongshu.com"
API_BASE_URL = "https://edith.xiaohongshu.com"

# 单例: 复用同一个浏览器进程
_browser_ctx: Optional[BrowserContext] = None
_browser_page: Optional[Page] = None
_playwright = None
_browser_loop: Optional[asyncio.AbstractEventLoop] = None


def ensure_loop() -> asyncio.AbstractEventLoop:
    """返回进程内复用的 event loop (浏览器单例挂在这个 loop 上)。

    所有浏览器操作 (ensure_browser / do_request / shutdown) 必须在
    同一个 loop 上执行, 否则 Playwright 会报 "attached to a different loop"。
    """
    global _browser_loop
    if _browser_loop is None or _browser_loop.is_closed():
        _browser_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_browser_loop)
    return _browser_loop


def _normalize_same_site(v):
    """Playwright 只接受 Strict/Lax/None; EditThisCookie 导出的 'unspecified'/null 按 Lax 处理。"""
    return v if v in ("Strict", "Lax", "None") else "Lax"


def load_cookies(path: str | Path) -> List[Dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else [{"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"} for k, v in raw.items()]
    out = []
    for c in items:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ".xiaohongshu.com"
        if domain and not domain.startswith("."):
            domain = "." + domain
        out.append({
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        })
    return out


async def ensure_browser() -> Page:
    global _browser_ctx, _browser_page, _playwright
    if _browser_page is not None:
        return _browser_page

    _playwright = await async_playwright().start()
    browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    _browser_ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 xhs-pc-web/6.45.1"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    await _browser_ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    cookies = load_cookies(COOKIES_FILE)
    if cookies:
        await _browser_ctx.add_cookies(cookies)
        LOG.info("injected %d cookies", len(cookies))
    page = await _browser_ctx.new_page()
    LOG.info("navigating to %s ...", DEFAULT_BASE_URL)
    await page.goto(f"{DEFAULT_BASE_URL}/explore", wait_until="networkidle", timeout=60000)
    # 等 SDK 加载
    try:
        await page.wait_for_function("typeof window.mnsv2 === 'function'", timeout=30000)
        LOG.info("window.mnsv2 loaded OK")
    except Exception as exc:
        LOG.warning("window.mnsv2 not detected: %s", exc)
    _browser_page = page
    return page


async def probe() -> Dict[str, Any]:
    page = await ensure_browser()
    info = await page.evaluate(
        """() => {
            const result = {
                url: location.href,
                cookies: document.cookie,
                hasMnsv2: typeof window.mnsv2 === 'function',
                hasSeccore: typeof window.seccore_signv2,
                hasWebSession: !!document.cookie.match(/web_session=/),
                title: document.title,
            };
            // 探测 fetch 拦截器是否已附加
            try {
                const desc = Object.getOwnPropertyDescriptor(window, 'fetch');
                result.fetchOverridden = !!(desc && desc.get && desc.get.toString().indexOf('native code') < 0);
            } catch(e) { result.fetchOverridden = null; }
            return result;
        }"""
    )
    return info


async def do_request(method: str, url: str, headers: Dict[str, str], body: Any) -> Dict[str, Any]:
    """在浏览器里发请求, 由浏览器拦截器自动加 X-s / X-t / X-common-params。"""
    if url.startswith("/"):
        url = API_BASE_URL + url
    page = await ensure_browser()
    body_str = json.dumps(body) if body is not None else None

    script = """
    async ({method, url, headers, body}) => {
        try {
            const init = { method, headers: {...headers}, credentials: 'include' };
            if (body != null) {
                init.body = body;
                if (typeof body === 'string' && !init.headers['Content-Type']) {
                    init.headers['Content-Type'] = 'application/json';
                }
            }
            const resp = await fetch(url, init);
            const text = await resp.text();
            return {
                status: resp.status,
                statusText: resp.statusText,
                headers: Object.fromEntries(resp.headers.entries()),
                body: text,
            };
        } catch (e) {
            return {error: String(e), stack: e.stack};
        }
    }
    """
    # 用浏览器内的 fetch 发请求, XHS 的全局拦截器 (含 fetch hook) 会自动加签名
    apiResp = await page.evaluate(script, {"method": method, "url": url, "headers": headers, "body": body_str})
    return apiResp


async def shutdown():
    global _browser_ctx, _browser_page, _playwright
    if _browser_ctx:
        try:
            await _browser_ctx.close()
        except Exception:
            pass
        _browser_ctx = None
        _browser_page = None
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


def shutdown_now() -> None:
    """同步关闭浏览器单例 (在浏览器所在的 event loop 上执行)。幂等。"""
    if _browser_ctx is None and _playwright is None:
        return
    try:
        ensure_loop().run_until_complete(shutdown())
        LOG.info("browser singleton shutdown OK")
    except Exception as exc:
        LOG.warning("shutdown 异常: %s", exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Playwright 驱动 XHS 浏览器抓取")
    parser.add_argument("--probe", action="store_true", help="只探测浏览器状态")
    parser.add_argument("--request", help="JSON 字符串: {method, url, headers, body}")
    parser.add_argument("--shutdown", action="store_true", help="关闭当前进程的浏览器单例 (幂等)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

    async def run():
        try:
            if args.probe:
                info = await probe()
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            if args.request:
                req = json.loads(args.request)
                resp = await do_request(
                    method=req.get("method", "GET"),
                    url=req["url"],
                    headers=req.get("headers", {}),
                    body=req.get("body"),
                )
                print(json.dumps(resp, ensure_ascii=False, indent=2))
                return 0
            if args.shutdown:
                shutdown_now()
                print("browser shutdown OK")
                return 0
            LOG.error("use --probe / --request / --shutdown")
            return 1
        finally:
            await shutdown()
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
