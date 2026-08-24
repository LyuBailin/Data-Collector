# -*- coding: utf-8 -*-
"""
capture_sdk.py — 用 Playwright 抓 XHS 动态 SDK JS

XHS 首页会通过 vendor.js 异步调用 /api/sec/v1/he 拿到 anti_hp_sign_config,
其中的 coreScriptUrl 才是真正提供 window.mnsv2 的 JS 文件。

本脚本:
  1. 启动 headless Chromium, 注入用户 cookie
  2. 打开 https://www.xiaohongshu.com/explore
  3. 拦截所有包含 mnsv2 / seccore / scripting 的 JS 响应
  4. 验证 window.mnsv2 已加载并可用
  5. 把 SDK JS 存到 assets/bundles/sdk.js, 同时 dump mnsv2 源码到 sdk-mnsv2.txt

CLI:
  python scripts/capture_sdk.py [--headed]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("需要安装 playwright: pip install playwright && playwright install chromium", file=sys.stderr)
    raise

LOG = logging.getLogger("capture_sdk")

COOKIES_FILE = "assets/cookies.json"
SDK_OUTPUT = "assets/bundles/sdk.js"
MNSV2_SRC_OUTPUT = "assets/bundles/sdk-mnsv2.txt"
PRIMARY_DOMAIN = ".xiaohongshu.com"


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
        domain = c.get("domain") or PRIMARY_DOMAIN
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


async def capture(headed: bool = False, timeout_ms: int = 60000) -> int:
    cookies = load_cookies(COOKIES_FILE)
    LOG.info("loaded %d cookies", len(cookies))
    if not cookies:
        LOG.error("cookie 文件为空或格式不对")
        return 2

    captured: List[Dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed,
            args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 xhs-pc-web/6.45.1"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 反检测
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        await context.add_cookies(cookies)

        page = await context.new_page()

        # 拦截所有相关响应
        async def on_response(response):
            url = response.url
            try:
                ct = response.headers.get("content-type", "")
                if response.status != 200:
                    return
                if "javascript" not in ct and not url.endswith(".js"):
                    return
                # 排除我们自己已知的 8 个首页 bundle
                skip_names = (
                    "sec_ds.js", "bundler-runtime", "library-polyfill",
                    "library-lodash", "vendor-dynamic", "vendor.25",
                    "index.b7fbd569", "04b29480233f4def", "a9ef723c54cfdb635",
                )
                if any(name in url for name in skip_names):
                    return
                # 关注 sec/sdk 相关的 JS
                is_sdk = any(kw in url for kw in ("sec/v1/", "sdk", "rap", "anti", "sec_", "/api/sec", "scripting", "sbtsource"))
                is_js = url.endswith(".js")
                if not (is_sdk or is_js):
                    return
                body = await response.body()
                captured.append({"url": url, "body": body, "ct": ct})
                LOG.info("captured: %s (%d bytes, ct=%s)", url, len(body), ct)
            except Exception as exc:
                LOG.warning("response hook failed for %s: %s", url, exc)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        LOG.info("navigating to https://www.xiaohongshu.com/explore ...")
        try:
            await page.goto("https://www.xiaohongshu.com/explore", wait_until="networkidle", timeout=timeout_ms)
        except Exception as exc:
            LOG.warning("navigation: %s", exc)

        # 触发搜索/页面交互以让 SDK 加载完毕
        try:
            await page.wait_for_function("typeof window.mnsv2 === 'function'", timeout=15000)
            LOG.info("window.mnsv2 loaded OK")
        except Exception as exc:
            LOG.warning("window.mnsv2 not detected: %s", exc)

        # 再等一会儿让任何延迟请求完成
        await asyncio.sleep(3)

        # 探针: 抓 window.mnsv2 源码和所有相关 global
        info = await page.evaluate(
            """() => {
                const fns = [];
                const candidates = ['mnsv2','mnsv1','mns','sbtsource','sec_sign','seccore_signv2','sign_v2','__sec_sign__'];
                for (const k of candidates) {
                    try {
                        const v = window[k];
                        if (v) fns.push({key: k, type: typeof v});
                    } catch(e) {}
                }
                let mnsv2Src = null;
                try { if (typeof window.mnsv2 === 'function') mnsv2Src = window.mnsv2.toString(); } catch(e) {}
                return { keys: fns, mnsv2Src, mnsv2Type: typeof window.mnsv2 };
            }"""
        )
        LOG.info("window.mnsv2 type: %s", info.get("mnsv2Type"))
        for f in info.get("keys") or []:
            LOG.info("  found: window.%s (%s)", f["key"], f["type"])

        await browser.close()

    if info.get("mnsv2Src"):
        Path(MNSV2_SRC_OUTPUT).write_text(info["mnsv2Src"], encoding="utf-8")
        LOG.info("mnsv2 source saved to %s (%d chars)", MNSV2_SRC_OUTPUT, len(info["mnsv2Src"]))

    if not captured:
        LOG.error("没抓到任何 JS")
        return 1

    # 保存所有捕获到的 JS 到 captured/ 子目录
    cap_dir = Path("assets/bundles/captured")
    cap_dir.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(captured):
        name = c["url"].split("/")[-1].split("?")[0] or f"chunk_{i}.js"
        out = cap_dir / f"{i:03d}_{name}"
        out.write_bytes(c["body"])
        LOG.info("captured[%d]: %s -> %s (%d bytes)", i, c["url"], out, len(c["body"]))

    # 找出含 mnsv2 函数体的 JS, 这是真正的 SDK
    sdk = None
    for c in captured:
        body_str = c["body"].decode("utf-8", errors="ignore")
        if "mnsv2" in body_str and ("_0x31ad27" in body_str or "_0x30754b" in body_str or "function _0x30ce91" in body_str):
            sdk = c
            break
    if not sdk:
        sdk = max(captured, key=lambda c: len(c["body"]), default=None)
    if not sdk:
        LOG.error("没抓到 SDK JS")
        return 1
    Path(SDK_OUTPUT).write_bytes(sdk["body"])
    LOG.info("SDK JS saved to %s (%d bytes from %s)", SDK_OUTPUT, len(sdk["body"]), sdk["url"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 Playwright 抓 XHS 动态 SDK JS")
    parser.add_argument("--headed", action="store_true", help="用有头模式 (调试用)")
    parser.add_argument("--timeout", type=int, default=60000, help="导航超时 (ms)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return asyncio.run(capture(headed=args.headed, timeout_ms=args.timeout))


if __name__ == "__main__":
    sys.exit(main())
