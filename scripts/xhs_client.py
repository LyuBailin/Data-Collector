# -*- coding: utf-8 -*-
"""
xhs_client.py - 小红书 web 接口底层客户端

职责:
  1. 加载本地 cookie (Chrome DevTools 导出格式 JSON)
  2. 注入 X-s / X-t / X-common-params 等请求头
  3. 内置限速 (随机 jitter)、失败退避、错误码识别
  4. 支持两种签名引擎:
       - legacy: 纯 Python 实现的旧版 XOR + base64 (依赖首页抽出的 msyw)
       - node:   调用本地 node.js 子进程运行真正的 XHS JS bundle (实验性, 见 references/signing.md)

CLI:
  python xhs_client.py init-cookie
  python xhs_client.py refresh-fingerprint
  python xhs_client.py whoami
  python xhs_client.py debug-sign <path> [key=value ...]
  python xhs_client.py sign-engine <node|legacy>
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

try:
    import requests
except ImportError:
    print("需要安装 requests: pip install requests", file=sys.stderr)
    raise

LOG = logging.getLogger("xhs_client")

DEFAULT_COOKIE_FILE = "assets/cookies.json"
DEFAULT_FP_CACHE = "assets/fingerprint.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 xhs-pc-web/6.45.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.xiaohongshu.com",
    "Referer": "https://www.xiaohongshu.com/",
}

BASE_HOST = "https://www.xiaohongshu.com"
HOMEPAGE_URL = f"{BASE_HOST}/explore"
SEARCH_URL = f"{BASE_HOST}/search_result"


# ----------------------------- cookie -----------------------------


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_cookies(path):
    """把 Chrome DevTools 导出的 cookies JSON 转成 dict。"""
    raw = _read_json(Path(path))
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v not in (None, "")}
    out = {}
    for c in raw:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        out[str(name)] = str(value)
    if not out:
        raise ValueError(f"cookie 文件 {path} 中没有可用条目")
    return out


def save_cookies(path, cookies):
    arr = [
        {
            "domain": ".xiaohongshu.com",
            "name": k,
            "value": v,
            "path": "/",
            "httpOnly": k in {"web_session", "id_token", "acw_tc", "x-rednote-datactry", "x-rednote-holderctry"},
            "secure": k.startswith("x-rednote") or k in {"web_session", "id_token"},
        }
        for k, v in cookies.items()
    ]
    _write_json(Path(path), arr)


# ----------------------------- legacy X-s -----------------------------


def sign_legacy(url_path, params, a1, msyw, ts=None):
    """旧版 X-s 签名 (XOR + base64)。

    适用于部分老版 / 内部端点; 当前 XHS web 已切到 seccore_signv2 算法,
    这里仅作为 fallback。
    """
    if ts is None:
        ts = int(time.time() * 1000)
    items = sorted((params or {}).items(), key=lambda kv: kv[0])
    query = urlencode(items, doseq=True, safe="*")
    base = f"{url_path}?{query}#{msyw}"
    xor_bytes = bytes((ord(c) ^ ord(a1[i % len(a1)])) & 0xFF for i, c in enumerate(base))
    x_s = base64.b64encode(xor_bytes[::-1]).decode("ascii")
    return x_s, str(ts)


def sign_node(node_script, url_path, params, body, a1, msyw, cookie_string=""):
    """通过 node 子进程调用真正的 XHS 签名实现 (实验性)。"""
    payload = {
        "path": url_path,
        "params": params or {},
        "body": body or {},
        "a1": a1,
        "msyw": msyw,
        "cookieString": cookie_string,
    }
    result = subprocess.run(
        ["node", node_script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sign_bridge.js 失败: {result.stderr.strip()}")
    out = json.loads(result.stdout.strip())
    if "x_s" not in out:
        raise RuntimeError(f"sign_bridge.js 返回无效: {out}")
    return out["x_s"], out["x_t"]


# ----------------------------- 指纹提取 -----------------------------


def fetch_fingerprint(session, force=False, fp_cache=DEFAULT_FP_CACHE):
    """从首页 HTML 抽取 window._webmsxyw 风格指纹 (旧版)。

    注意: 当前 XHS web 已不直接输出 _webmsxyw,
    本函数会尝试多种规则; 若全部失败, 抛 RuntimeError, 提示用户使用 node 签名引擎。
    """
    cache_path = Path(fp_cache)
    if not force and cache_path.exists():
        try:
            cached = _read_json(cache_path)
            if cached.get("webmsxyw") and (time.time() - cached.get("fetched_at", 0)) < 6 * 3600:
                LOG.info("复用首页指纹缓存 (%s)", cache_path)
                return cached
        except Exception as exc:
            LOG.warning("读取指纹缓存失败: %s", exc)

    LOG.info("拉取首页以抽取 _webmsxyw / sec salt ...")
    resp = session.get(HOMEPAGE_URL, headers=DEFAULT_HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text

    candidates = [
        r"window\._webmsxyw\s*=\s*['\"]([A-Za-z0-9_\-]+)['\"]",
        r'"webmsxyw"\s*:\s*"([A-Za-z0-9_\-]+)"',
    ]
    webmsxyw = ""
    for pat in candidates:
        m = re.search(pat, html)
        if m:
            webmsxyw = m.group(1)
            break

    # 备选: 取 INITIAL_STATE 里的 salt-ish 字段
    if not webmsxyw:
        m = re.search(r'"ets"\s*:\s*"?(\d{13,16})"?', html)
        if m:
            webmsxyw = m.group(1)

    if not webmsxyw:
        raise RuntimeError(
            "无法从首页抽取 _webmsxyw —— 当前 XHS web 已切换到 seccore_signv2 算法, "
            "需要使用 node 签名引擎 (见 references/signing.md)。"
        )

    data = {
        "webmsxyw": webmsxyw,
        "fetched_at": time.time(),
        "html_size": len(html),
        "url": HOMEPAGE_URL,
    }
    _write_json(cache_path, data)
    LOG.info("指纹已缓存到 %s", cache_path)
    return data


# ----------------------------- Client -----------------------------


class XHSClient:
    """小红书 web 接口 HTTP 客户端。"""

    SIGN_ENGINES = ("legacy", "node", "browser")

    def __init__(
        self,
        cookie_file=DEFAULT_COOKIE_FILE,
        fp_cache=DEFAULT_FP_CACHE,
        min_delay=1.0,
        max_delay=2.0,
        max_retries=3,
        sign_engine="browser",
        node_sign_script="scripts/sign_bridge.js",
    ):
        self.cookie_file = cookie_file
        self.fp_cache = fp_cache
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.sign_engine = sign_engine
        self.node_sign_script = node_sign_script
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.cookies = {}
        self.fingerprint = {}
        self._loaded = False

    def load(self, force_fp=False):
        self.cookies = load_cookies(self.cookie_file)
        self.session.cookies.clear()
        for k, v in self.cookies.items():
            self.session.cookies.set(k, v, domain=".xiaohongshu.com", path="/")
        if self.sign_engine == "browser":
            # 浏览器引擎不需要本地指纹, 由浏览器拦截器生成签名
            self.fingerprint = {}
            self._loaded = True
            LOG.info("已加载 %d 个 cookie, sign_engine=browser (用 Playwright 在浏览器里发请求)", len(self.cookies))
            return
        try:
            self.fingerprint = fetch_fingerprint(self.session, force=force_fp, fp_cache=self.fp_cache)
        except RuntimeError as exc:
            LOG.warning("指纹提取失败: %s", exc)
            self.fingerprint = {}
        self._loaded = True
        LOG.info(
            "已加载 %d 个 cookie, sign_engine=%s, webmsxyw=%s",
            len(self.cookies),
            self.sign_engine,
            (self.fingerprint.get("webmsxyw", "") or "")[:12] + "...",
        )

    def ensure_loaded(self):
        if not self._loaded:
            self.load()

    def _sleep(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _build_common_params(self):
        a1 = self.cookies.get("a1", "")
        webId = self.cookies.get("webId", "")
        gid = self.cookies.get("gid", "")
        webBuild = self.cookies.get("webBuild", "6.45.1")
        return {
            "deviceParams": json.dumps(
                {
                    "appId": "xhs-pc-web",
                    "appVersion": webBuild,
                    "build": webBuild,
                    "deviceId": webId,
                    "fid": gid,
                    "identifier": webId,
                    "platform": "web",
                    "sid": self.cookies.get("web_session", ""),
                    "userId": "",
                    "versionName": webBuild,
                },
                separators=(",", ":"),
            ),
            "xsecAppid": "xhs-pc-web",
            "xsecToken": self.cookies.get("websectiga", ""),
            "source": "web_search_result",
            "sid": self.cookies.get("web_session", ""),
        }

    def _sign(self, url_path, params, body):
        # browser / node 引擎都通过浏览器发请求, 不用预生成签名
        return ("browser-managed", str(int(time.time() * 1000)))

    def _headers(self, url_path, params, body):
        x_s, x_t = self._sign(url_path, params, body)
        if x_s is None:
            # node bridge fell back to legacy; raise so caller sees the situation
            raise RuntimeError("X-s 签名失败: 请检查 cookie 是否有效, 并阅读 references/signing.md")
        return {
            "X-s": x_s,
            "X-t": x_t,
            "X-common-params": json.dumps(self._build_common_params(), separators=(",", ":")),
            "X-web-s": x_s,
            "X-web-t": x_t,
            "Content-Type": "application/json;charset=UTF-8",
        }

    def _do_browser_request(self, method, url, params=None, json_body=None, attempt=0):
        """通过 Playwright 浏览器发请求 (浏览器拦截器自动加签名)。

        与 _request 相同语义:
          - HTTP 401/403 / code=-101 / code=300011 -> 抛 RuntimeError (cookie 问题, 立刻停止)
          - code=-102 -> 等 60s 重试 (最多 max_retries 次)
          - 其余 code != 0 -> 返回 success=False, 由调用方判断
        """
        import json as _json
        from urllib.parse import urlencode
        from playwright_driver import ensure_browser, ensure_loop, do_request as _do_request
        loop = ensure_loop()

        async def _run():
            await ensure_browser()
            full_url = url
            if params:
                full_url = url + "?" + urlencode(params, doseq=True)
            resp = await _do_request(method, full_url, {}, json_body)
            status = resp.get("status", 0)
            body_str = resp.get("body", "")
            if resp.get("error"):
                raise RuntimeError(f"浏览器内请求失败: {resp.get('error')}")
            try:
                data = _json.loads(body_str) if body_str else {}
            except Exception:
                data = {"_raw": body_str}
            return status, data

        while True:
            status, data = loop.run_until_complete(_run())

            if status in (401, 403):
                raise RuntimeError(f"HTTP {status} —— 签名失败或被拒, 请检查 cookie 是否完整, 必要时重新启动 Chromium")

            if status == 200 and isinstance(data, dict):
                code = data.get("code")
                if code not in (None, 0):
                    msg = str(data.get("msg") or "")
                    if code in (-101,) or "login" in msg.lower():
                        raise RuntimeError("登录态失效 (code=-101), 请重新提供 cookie")
                    if code == 300011:
                        raise RuntimeError(f"账号状态异常 (code=300011): {msg} —— 请换一组新鲜 cookie 后重试")
                    if code == -102 and attempt < self.max_retries:
                        attempt += 1
                        LOG.warning("风控触发 (code=-102), 等待 60s 后重试 (第 %d 次)", attempt)
                        time.sleep(60)
                        continue
                    return {"code": code, "success": False, "msg": msg,
                            "data": data.get("data") or {}, "_status": status}
                return {"code": 0, "success": True, "data": data, "_status": status}

            return {"code": -1, "success": False, "msg": f"HTTP {status} / 非 JSON 响应",
                    "data": data, "_status": status}

    def get(self, url, params=None, referer=None):
        if self.sign_engine == "browser":
                return self._do_browser_request("GET", url, params=params)
        return self._request("GET", url, params=params, json_body=None, referer=referer)

    def post(self, url, params=None, body=None, referer=None):
        if self.sign_engine == "browser":
                return self._do_browser_request("POST", url, params=params, json_body=body or {})
        return self._request("POST", url, params=params, json_body=body or {}, referer=referer)

    def _request(self, method, url, params, json_body, referer):
        self.ensure_loaded()
        parsed = url if url.startswith("http") else f"{BASE_HOST}{url}"
        url_path = urlparse(parsed).path

        attempt = 0
        last_exc = None
        while attempt < self.max_retries:
            attempt += 1
            headers = self._headers(url_path, params or {}, json_body or {})
            headers["Referer"] = referer or SEARCH_URL
            try:
                if method == "GET":
                    resp = self.session.get(parsed, params=params, headers=headers, timeout=15)
                else:
                    resp = self.session.post(parsed, params=params, json=json_body, headers=headers, timeout=15)
            except requests.RequestException as exc:
                last_exc = exc
                LOG.warning("[%s] %s 失败 (第 %d 次): %s", method, parsed, attempt, exc)
                time.sleep(min(30, 2 ** attempt))
                continue

            if resp.status_code in (401, 403):
                raise RuntimeError(f"HTTP {resp.status_code} —— cookie 可能已失效, 请重新提供")

            try:
                data = resp.json()
            except ValueError:
                LOG.warning("[%s] %s 返回非 JSON: %s", method, parsed, resp.text[:200])
                time.sleep(2)
                continue

            code = data.get("code")
            success = data.get("success", True)
            if code in (0, None) and success:
                self._sleep()
                return data

            msg = (data.get("msg") or data.get("message") or "").lower()
            if "login" in msg or code in (-101,):
                raise RuntimeError("登录态失效 (code=-101), 请重新提供 cookie")
            if "account.frozen" in msg or "frozen" in msg:
                raise RuntimeError("账号被风控冻结, 请停止抓取")
            if code == -102:
                LOG.warning("风控触发, 等待 60s 后重试 (第 %d 次)", attempt)
                time.sleep(60)
                continue
            if code == 404 or "path invalid" in msg:
                raise RuntimeError(f"接口路径失效: {data}")
            LOG.warning("[%s] %s code=%s msg=%s, 重试中", method, parsed, code, data.get("msg"))
            time.sleep(min(30, 2 ** attempt))

        if last_exc:
            raise last_exc
        raise RuntimeError(f"{method} {parsed} 多次重试仍失败")


# ----------------------------- CLI -----------------------------


def cmd_init_cookie(args):
    cookies = load_cookies(args.cookie_file)
    save_cookies(args.cookie_file, cookies)
    cli = XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        sign_engine=args.sign_engine,
        node_sign_script=args.node_sign_script,
    )
    try:
        cli.load(force_fp=True)
    except RuntimeError as exc:
        print(f"[warn] {exc}")
    print(f"OK - {len(cookies)} cookies, sign_engine={cli.sign_engine}")


def cmd_refresh_fingerprint(args):
    cli = XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        sign_engine=args.sign_engine,
        node_sign_script=args.node_sign_script,
    )
    try:
        cli.load(force_fp=True)
    except RuntimeError as exc:
        print(f"[warn] {exc}")
    print(f"OK - webmsxyw={(cli.fingerprint.get('webmsxyw', '') or '')[:12]}...")


def cmd_whoami(args):
    cli = XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        sign_engine=args.sign_engine,
        node_sign_script=args.node_sign_script,
    )
    cli.load()
    cookies = cli.cookies
    out = {
        "a1": cookies.get("a1", "")[:16] + "...",
        "web_session_prefix": (cookies.get("web_session") or "")[:10],
        "webId": cookies.get("webId", "")[:12],
        "sign_engine": cli.sign_engine,
    }
    if cli.fingerprint.get("webmsxyw"):
        out["webmsxyw"] = cli.fingerprint["webmsxyw"][:12] + "..."
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_debug_sign(args):
    cli = XHSClient(
        cookie_file=args.cookie_file,
        fp_cache=args.fp_cache,
        sign_engine=args.sign_engine,
        node_sign_script=args.node_sign_script,
    )
    cli.load()
    params = {}
    for kv in args.params:
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
    x_s, x_t = cli._sign(args.path, params, body={})
    print(json.dumps({"path": args.path, "params": params, "X-s": x_s, "X-t": x_t}, ensure_ascii=False, indent=2))


def build_parser():
    p = argparse.ArgumentParser(description="小红书底层 HTTP 客户端")
    p.add_argument("--cookie-file", default=DEFAULT_COOKIE_FILE)
    p.add_argument("--fp-cache", default=DEFAULT_FP_CACHE)
    p.add_argument("--sign-engine", choices=XHSClient.SIGN_ENGINES, default="browser")
    p.add_argument("--node-sign-script", default="scripts/sign_bridge.js")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("init-cookie", help="加载 cookie 并刷新首页指纹")
    s1.set_defaults(func=cmd_init_cookie)

    s2 = sub.add_parser("refresh-fingerprint", help="强制重新拉首页抽取 _webmsxyw")
    s2.set_defaults(func=cmd_refresh_fingerprint)

    s3 = sub.add_parser("whoami", help="显示当前 cookie 与指纹摘要")
    s3.set_defaults(func=cmd_whoami)

    s4 = sub.add_parser("debug-sign", help="打印指定 path+params 的 X-s/X-t, 不发起请求")
    s4.add_argument("path")
    s4.add_argument("params", nargs="*", help="key=value 列表, 例如 keyword=露营 page=1")
    s4.set_defaults(func=cmd_debug_sign)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return args.func(args) or 0
    except Exception as exc:
        LOG.error("执行失败: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
