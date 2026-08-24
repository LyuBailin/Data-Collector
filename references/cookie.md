# Cookie 使用与维护说明

## 数据格式
`assets/cookies.json` 采用 Chrome DevTools `Cookies` 面板导出的 JSON 结构（数组，每项含 `name`、`value`、`domain`、`path`、`httpOnly`、`secure` 等字段）。本 skill 不要求完整字段；至少需要以下键：

| 名称 | 用途 | 必需 |
| --- | --- | --- |
| `a1` | 设备指纹，参与 X-s 签名 | 是 |
| `web_session` | 鉴权态 | 是 |
| `id_token` | 鉴权态 | 是 |
| `webId` | 设备 / 行为 id | 推荐 |
| `gid` | 全局 id | 推荐 |
| `acw_tc` | 阿里风控 | 推荐 |
| `websectiga` | 行为指纹 | 推荐 |
| `xsecappid` | app 标识，默认 `xhs-pc-web` | 否 |
| `webBuild` | 前端构建号（参与前端校验） | 否 |
| `ets` / `loadts` | 起始时间戳 | 否 |
| `unread` | UI 计数器（红点） | 否 |
| `x-rednote-datactry` / `x-rednote-holderctry` | 区域设置（CN） | 否 |

## 更新流程
1. 在已登录小红书的 Chrome 中打开 DevTools → Application → Cookies → `https://www.xiaohongshu.com`。
2. 全部选中 → 右键 → “Show All Cookies” 或使用 EditThisCookie / Cookie-Editor 扩展导出 JSON。
3. 把 JSON 覆盖写入 `assets/cookies.json`。
5. 运行 `python scripts/xhs_client.py --init-cookie assets/cookies.json` 校验可解析并刷新 `_webmsxyw`。

## 安全与合规
- 仅在用户提供的 cookie 上抓取本人账号可见的数据；不要把 cookie 写入 git、聊天记录、共享位置。
- 一旦 cookie 失效或返回 `code = -101 / login_required / account.frozen`，请立刻终止所有抓取任务并提示用户重新提供。
- 不要并发或暴力抓取；XHS 风控会把高频 IP / 设备指纹直接封号。

## 失效信号
- 响应 JSON 中 `code` 字段返回：
  - `-101`：未登录 / session 失效
  - `-102`：风控触发
  - `404`：接口下线或路径变更
  - `account.frozen`：账号被冻结
  - `login_required`：需要重新登录
- 抓取失败或内容突然大幅减少，都是 cookie 即将失效的预警。
