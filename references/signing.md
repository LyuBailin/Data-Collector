# X-s / X-t 签名说明

## TL;DR

XHS web 接口的所有 API 请求都带 `X-s` / `X-t` / `X-common-params` / `X-mns` 头部, 缺少或错误会被服务端直接拒绝。这套算法是 XHS 反爬的核心, 一直在演进。

`xhs_client.py` 通过 `--sign-engine` 选择签名实现:

| 引擎 | 状态 | 说明 |
| --- | --- | --- |
| `browser` | ✅ 默认 | 用 Playwright 启动 Chromium, 注入 cookie, 加载 `xiaohongshu.com`, 让浏览器自己的 axios 拦截器自动加签名。最稳。 |
| `node`   | 🚧 实验性 | 把首页 JS bundle 加载到 node.js vm 沙箱里, 调用真实 `seccore_signv2`。当前 SDK 字节码在 vm 里还有兼容问题。 |
| `legacy` | ✅ 内置 | 旧版 `XOR + base64`, 已被 XHS 废弃, 仅供教学。 |

> **当前实战**: 默认 `--sign-engine browser`。你给一组有效 cookie, 跑 `python scripts/collect.py --keyword ...` 就能拿到真实数据。

---

## 1. browser 引擎 (默认, 推荐)

### 1.1 工作原理

XHS web 端的 axios 拦截器在每次请求前自动加 `X-s` / `X-t` / `X-common-params` / `X-mns`。这个拦截器:
1. 加载首页 JS bundle (vendor / library / index) 时被注入
2. 拦截器里调用 `seccore_signv2(url, body)` 得到 X-s
3. `seccore_signv2` 调用私有 `window.mnsv2(c, u, p)` 和 `buildEncSskSign(u)`
4. `window.mnsv2` 由另一个动态 SDK (`/as/v2/ds/<hash>.js`) 注入, 包含私有的字节码解释器和私有算法

整个链路依赖浏览器运行时私有变量 (cookie、UA、salt、window.mnsv2 等), 在 node.js 沙箱里完整复现非常痛苦。

**所以选择让浏览器自己处理签名**, 用 Playwright 在 Chromium 里发请求。

### 1.2 工作流

```
collect.py  --sign-engine browser
    │
    ▼
XHSClient.post(url, body)
    │
    ▼
_do_browser_request(method, url, params, body)
    │
    ▼
playwright_driver.do_request(method, full_url, headers, body)
    │
    ▼
page.evaluate(async ({method, url, headers, body}) => {
    return await fetch(url, {method, headers, credentials: "include", body});
});
    │
    ▼ (Chromium 内 axios 拦截器注入 X-s / X-t / X-common-params)
    │
    ▼
edith.xiaohongshu.com/api/...
    │
    ▼
HTTP 200 / JSON 响应
```

### 1.3 安装

```bash
conda activate data-collect
pip install playwright
python -m playwright install chromium
```

### 1.4 验证

```bash
# 探针: 检查浏览器内 window.mnsv2 是否加载
python scripts/playwright_driver.py --probe

# 真实请求
python scripts/playwright_driver.py --request '{"method":"POST","url":"/api/sns/web/v1/search/notes","headers":{},"body":{"keyword":"露营","page":1,"page_size":3,"sort":"general","note_type":0,"search_id":""}}'
```

期望响应 (成功):
```json
{
  "status": 200,
  "headers": {"content-type": "application/json; ..."},
  "body": "{\"code\":0,\"success\":true,\"data\":{...},\"msg\":\"success\"}"
}
```

期望响应 (账号异常):
```json
{
  "status": 200,
  "body": "{\"code\":300011,\"success\":false,\"msg\":\"当前账号状态异常，请切换账号后重试\",\"data\":{}}"
}
```

### 1.5 单例 BrowserContext

`scripts/playwright_driver.py` 在进程内维护一个 `_browser_ctx` 单例。多次抓取复用同一个 Chromium 进程, 跳过冷启动。

---

## 2. node 引擎 (实验性)

**不推荐用于生产**, 仅作调试 / 学习用途。

### 2.1 已完成部分

我们做了这些 (写在这里记录历史):

* 把 9 个首页 JS bundle 全部加载到 node.js `vm` 沙箱
* 修补 webpack runtime 暴露 `__webpack_require__` 到 `globalThis`
* 修补 vendor-dynamic 模块 59527 注入 `r.d(a, {__sec: () => seccore_signv2})`
* 加载 `assets/bundles/sdk.js` (动态 SDK, 定义 `window.mnsv2`)
* `__webpack_require__(59527).__sec(url, body)` 真实跑通到 `window.mnsv2(c, u, p)` 调用

### 2.2 卡点

`window.mnsv2` 在沙箱里的 bytecode 执行时会尝试 `new MutationObserver()` 或 `new Performance()` 等浏览器原生构造函数。我们的 mock 不完全匹配, 会报 `_0x795c5c[_0x17e91c] is not a constructor`。

要解决需要给沙箱补齐 `Document` / `Performance` / `MutationObserver` / `Window` 等多个原生的全局类。可以用 `jsdom` (在某些 Python 环境装不上) 或自己实现 stub。

### 2.3 已废弃

不要再在生产链路里用 `--sign-engine node`, 改用 `browser`。

---

## 3. legacy 引擎 (教学)

旧版 XHS web 用的 XOR + base64 签名, 算法在 `scripts/xhs_client.py::sign_legacy`:

```
ts   = int(time.time() * 1000)
base = url_path + "?" + urlencoded(sorted_params) + "#" + msyw
xor  = base[i] ^ a1[i % len(a1)]  (字节级)
x_s  = base64(xor[::-1])
x_t  = str(ts)
```

依赖首页 HTML 里的 `window._webmsxyw`, 现已不再输出, 服务端不接受。

---

## 4. 何时报哪种错

| 错误码 / HTTP | 含义 | 处理 |
| --- | --- | --- |
| `code: 0` | 成功 | 正常解析 data |
| `code: -101` / `login_required` | cookie 失效或账号未登录 | 提示重新登录导出 cookie |
| `code: 300011` | "当前账号状态异常" | 提示账号被风控, 等几小时或换号 |
| `code: -102` | 风控触发 | sleep 60s 重试 |
| `code: 300012` | 网络异常 | 检查网络 |
| `code: 300013` | 访问频次异常 | 降低抓取频率 |
| `code: 300015` | 浏览器异常 | 通常是请求结构错, 检查 URL 和 body |
| HTTP 401/403 | 签名失败或被拒 | 重新启动 Chromium, 检查 cookie 完整性 |
| HTTP 500 `create invoker failed` | XHS 后端微服务挂了 | 等等再试, 不是你的问题 |
| HTTP 406 `x-kong-sign: 1` | Kong 网关拒绝签名 | 检查 Referer / method 是否合规 |

---

## 5. 抓 SDK JS 的一次性步骤 (用于重新抓 SDK 时)

`assets/bundles/` 里有所有需要的 JS, 如果 XHS 改版需要重新抓:

```bash
python scripts/capture_sdk.py
```

会启动 Chromium, 访问 `xiaohongshu.com/explore`, 拦截所有 JS 响应, 把含 `mnsv2` 的 JS 保存到 `assets/bundles/sdk.js` (替换旧的)。

输出:
```
captured: https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web (59828 bytes, ct=application/javascript;charset=utf-8)
captured: ... async/8019.6ff0577f.js (516 bytes)
... (21+ 个 JS)
SDK JS saved to assets/bundles/sdk.js (61927 bytes)
```
