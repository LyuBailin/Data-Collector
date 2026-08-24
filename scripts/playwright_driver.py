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
    """在浏览器里发请求, 由浏览器拦截器自动加 X-s / X-t / X-common-params。

    注意: raw fetch 只能拿到 fetch hook 自动附加的签名头; 部分网关
    (so.xiaohongshu.com 搜索服务) 会要求页面 axios 完整链路产生的额外头
    (x-s-common 等), raw fetch 可能被 406 拒绝。对这类接口请用
    page_search_notes / page_note_detail 页面驱动方式采集。
    """
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


# ------------------------- 页面驱动采集 -------------------------
# raw fetch 拿不到页面 axios 完整链路附加的签名头 (x-s-common 等),
# 搜索网关 (so.xiaohongshu.com) 会直接 406。这里改为"让页面自己发请求,
# 我们拦截页面自身的 API 响应", 与真实用户在浏览器里的行为完全一致。


async def page_search_notes(keyword: str, pages: int = 1, page_size: int = 20) -> List[Dict[str, Any]]:
    """页面驱动搜索: 打开搜索页, 捕获页面自身发出的 v2 search 响应。

    返回按出现顺序去重后的 items 列表 (每项含 note_card / id / model_type)。
    页面自行生成 search_id / session_id / 签名, 分页通过滚动触发。
    """
    from urllib.parse import quote

    page = await ensure_browser()
    captured: List[Dict[str, Any]] = []
    seen_ids: set = set()

    async def on_response(resp):
        url = resp.url
        if "so.xiaohongshu.com/api/sns/web/v2/search/notes" not in url:
            return
        try:
            payload = json.loads(await resp.text())
        except Exception:
            return
        data = payload.get("data") or {}
        for it in data.get("items") or []:
            if it.get("model_type") != "note":
                continue
            note = it.get("note_card") or it
            nid = note.get("note_id") or note.get("id") or it.get("id")
            if nid and nid in seen_ids:
                continue
            if nid:
                seen_ids.add(nid)
            captured.append(it)

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    search_url = f"{DEFAULT_BASE_URL}/search_result?keyword={quote(keyword)}&source=web_search_result"
    LOG.info("page_search_notes: goto %s (pages=%d)", search_url, pages)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        LOG.warning("page_search_notes goto: %s", exc)

    # 等第一页结果 (最长 ~20s)
    for _ in range(40):
        if captured:
            break
        await page.wait_for_timeout(500)
    if not captured:
        raise RuntimeError(f"搜索页未返回任何结果 (keyword={keyword}) —— 可能触发风控, 请稍后重试")

    # 滚动触发后续页加载
    for _ in range(max(0, pages - 1)):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3500)
    LOG.info("page_search_notes: captured %d note items", len(captured))
    return captured


async def page_note_detail(note_id: str, xsec_token: str = "", max_comment_pages: int = 1) -> Dict[str, Any]:
    """页面驱动: 打开笔记详情页, 从 INITIAL_STATE 提取笔记, 捕获评论 API 响应。

    返回 {"note": <INITIAL_STATE 原始 note dict>, "comments": [原始评论 dict 列表]}。
    笔记正文由页面 SSR 注入 window.__INITIAL_STATE__.note.noteDetailMap[id].note;
    评论由页面自身发出的 edith v2/comment/page 响应捕获。
    """
    page = await ensure_browser()
    comments: List[Dict[str, Any]] = []

    async def on_response(resp):
        url = resp.url
        if "api/sns/web/v2/comment/page" not in url or note_id not in url:
            return
        try:
            payload = json.loads(await resp.text())
        except Exception:
            return
        data = payload.get("data") or {}
        for c in data.get("comments") or []:
            comments.append(c)

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    url = f"{DEFAULT_BASE_URL}/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source=pc_search&source=web_live_list"
    LOG.info("page_note_detail: goto %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        LOG.warning("page_note_detail goto: %s", exc)
    await page.wait_for_timeout(6000)

    note = await page.evaluate(
        """(noteId) => {
            const s = window.__INITIAL_STATE__ || {};
            const m = (s.note || {}).noteDetailMap || {};
            return m[noteId] ? (m[noteId].note || null) : null;
        }""",
        note_id,
    )
    if not note:
        raise RuntimeError(f"笔记详情页未找到 noteId={note_id} (可能 xsec_token 缺失、笔记不可见或页面未加载)")

    # 滚动触发更多评论加载 (尽力而为)
    for _ in range(max(0, max_comment_pages - 1)):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3500)
    LOG.info("page_note_detail: note=%s comments=%d", note_id, len(comments))
    return {"note": note, "comments": comments}


async def page_user_posted(user_id: str, pages: int = 1) -> List[Dict[str, Any]]:
    """页面驱动: 打开用户主页, 从 INITIAL_STATE.user.notes 提取笔记。

    当前 web 端用户主页笔记是 SSR 注入的 (不发 user/posted API);
    滚动触发更多后重新读取并去重。
    """
    page = await ensure_browser()

    async def _read_notes():
        return await page.evaluate(
            """() => {
                const unref = (v) => v && v.__v_isRef
                    ? (v._rawValue !== undefined ? v._rawValue : v._value) : v;
                const u = (window.__INITIAL_STATE__ || {}).user || {};
                const notes = unref(u.notes);
                if (!Array.isArray(notes)) return [];
                const seen = new WeakSet();
                const safe = (o) => {
                    if (o === undefined || o === null) return null;
                    const s = JSON.stringify(o, (k, v) => {
                        if (typeof v === 'object' && v !== null) {
                            if (seen.has(v)) return undefined;
                            seen.add(v);
                        }
                        return v;
                    });
                    return s ? JSON.parse(s) : null;
                };
                return notes.map(x => (Array.isArray(x) ? x[0] : x))
                            .map(safe).filter(Boolean);
            }"""
        ) or []

    url = f"{DEFAULT_BASE_URL}/user/profile/{user_id}"
    LOG.info("page_user_posted: goto %s (pages=%d)", url, pages)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        LOG.warning("page_user_posted goto: %s", exc)
    await page.wait_for_timeout(6000)

    seen_ids: set = set()
    captured: List[Dict[str, Any]] = []

    def _merge(notes):
        for it in notes:
            if not isinstance(it, dict):
                LOG.warning("page_user_posted: 跳过非 dict 条目 (%s)", type(it).__name__)
                continue
            nid = it.get("note_id") or it.get("id")
            if nid and nid in seen_ids:
                continue
            if nid:
                seen_ids.add(nid)
            captured.append(it)

    _merge(await _read_notes())
    if not captured:
        raise RuntimeError(f"用户主页未返回笔记 (user_id={user_id}) —— 可能触发风控或用户不存在, 请稍后再试")

    for _ in range(max(0, pages - 1)):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(3500)
        _merge(await _read_notes())
    LOG.info("page_user_posted: captured %d notes", len(captured))
    return captured


async def page_hotlist() -> List[Dict[str, Any]]:
    """页面驱动: 打开热门榜页, 拦截页面自身发出的 hotlist 响应。"""
    page = await ensure_browser()
    captured: List[Dict[str, Any]] = []

    async def on_response(resp):
        url = resp.url
        if "api/sns/web/" not in url or "hotlist" not in url:
            return
        try:
            payload = json.loads(await resp.text())
        except Exception:
            return
        for it in (payload.get("data") or {}).get("items") or []:
            captured.append(it)

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    url = f"{DEFAULT_BASE_URL}/hotlist"
    LOG.info("page_hotlist: goto %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        LOG.warning("page_hotlist goto: %s", exc)
    for _ in range(40):
        if captured:
            break
        await page.wait_for_timeout(500)
    if not captured:
        raise RuntimeError("热门榜页未返回数据 —— 可能触发风控或页面路径变更, 请稍后再试")
    LOG.info("page_hotlist: captured %d items", len(captured))
    return captured


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
