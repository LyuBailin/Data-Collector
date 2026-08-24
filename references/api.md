# 小红书 web 接口参考

> **声明**：以下端点基于对 `xiaohongshu.com` web 端公开网络面板的反向整理，仅用于在用户已登录且本人拥有 / 已授权访问数据的前提下使用。本 skill 不发起绕过登录态的请求。

## 通用约定

| 名称 | 值 |
| --- | --- |
| Host | `https://www.xiaohongshu.com`（PC Web） |
| UA | `Mozilla/5.0 ... Chrome/124 Safari/537.36 xhs-pc-web 6.45.1` |
| Origin | `https://www.xiaohongshu.com` |
| Referer | 同请求页面 URL |
| `X-s` / `X-t` | 由 `scripts/xhs_client.py` 计算并注入 |
| `X-common-params` | JSON 字符串（`{deviceParams, sid, source, ...}`），由 cookie 中 `webBuild`、`a1` 等字段组合 |

## 端点清单 (2026-08 实测)

> **重要**: 笔记搜索接口已迁移。当前 web 端搜索走
> `so.xiaohongshu.com/api/sns/web/v2/search/notes`, 旧版
> `edith.xiaohongshu.com/api/sns/web/v1/search/notes` 返回 `code:300011`
> (废弃接口拒答, 并非账号风控)。v2 搜索/评论只能通过**页面驱动**方式采集
> (打开搜索页/笔记页, 拦截页面自身请求), raw fetch 会被 Kong 网关 406 拒绝。

| 用途 | Method | Host / Path | 关键参数 |
| --- | --- | --- | --- |
| 笔记搜索 (当前) | POST | `so.xiaohongshu.com/api/sns/web/v2/search/notes` | body: `keyword, page, page_size, search_id, sort, note_type, ext_flags:[], geo:"", image_formats:[...], session_id` |
| 笔记搜索 (旧, 已废弃) | POST | `edith.xiaohongshu.com/api/sns/web/v1/search/notes` | 返回 300011 |
| 用户搜索 | POST | `/api/sns/web/v1/search/users` | `keyword`, `page`, `page_size` |
| 热门榜 | POST | `/api/sns/web/v1/search/hotlist` | `category`, `page_size` |
| 首页 feed 推荐 | POST | `/api/sns/web/v1/feed` | `cursor_score`, `num` |
| 单篇笔记详情 (页面驱动) | GET | `edith.../api/sns/web/v1/feed` | `source_note_id`, `xsec_token`, `xsec_source`, `source`; 或直接读笔记页 `window.__INITIAL_STATE__.note.noteDetailMap[id].note` |
| 用户基本信息 | GET | `/api/sns/web/v1/user/other_info` | `target_user_id` |
| 用户笔记列表 | GET | `/api/sns/web/v1/user/posted` | `user_id`, `cursor`, `num` |
| 用户收藏笔记 | GET | `/api/sns/web/v1/user/collect` | `user_id`, `cursor`, `num` |
| 笔记评论 (当前) | GET | `edith.../api/sns/web/v2/comment/page` | `note_id`, `cursor`, `top_comment_id`, `xsec_token` |
| 笔记评论 (旧) | GET | `/api/sns/web/v1/comment/page` | `note_id`, `cursor`, `top_comment_id` |
| 子评论 | GET | `/api/sns/web/v1/comment/sub/page` | `note_id`, `root_comment_id`, `cursor` |
| 话题笔记列表 | POST | `/api/sns/web/v1/page_topic/notes` | `topic_id`, `page_size`, `cursor` |

### v2 搜索响应字段差异 (相对 v1)

- 外层 item: `{id, model_type:"note", note_card:{...}, xsec_token}`, note_card 里**没有** `note_id` (在 item.id)。
- 标题字段是 `display_title` (不是 `title`); 搜索卡片**不含 `desc` / tags / 时间戳** (只有 `corner_tag_info` 里的 `publish_time` "MM-DD")。
- 互动字段: `interact_info.liked_count / collected_count / comment_count / shared_count` (字符串)。
- 完整正文 / 标签 / 时间戳需要进笔记详情页 (`__INITIAL_STATE__`, 字段为 camelCase: `noteId / interactInfo.likedCount / tagList[].name / time / lastUpdateTime / xsecToken`)。

## X-s / X-t 签名

```
ts  = int(time.time() * 1000)
url_path = api path (不含 host / query)
params   = {k=v} 字典
a1       = cookie 中的 a1 值
msyw     = window._webmsxyw  (从首页 HTML 抽取)

raw = url_path + "?" + "&".join(sorted(k=v for k,v in params.items())) + "#" + msyw
xor = "".join(chr(ord(c) ^ ord(a1[i % len(a1)])) for i,c in enumerate(raw))
rev = xor[::-1].encode("latin-1")
x_s = base64.b64encode(rev).decode()
x_t = str(ts)
```

请求头携带：
```
X-s: {x_s}
X-t: {x_t}
X-common-params: {"deviceParams":..., "sid":"web_session...", "source":"web"}
```

## 状态码

| 字段 | 含义 |
| --- | --- |
| `code == 0` | 成功 |
| `code != 0 && success: false` | 失败，查看 `msg` 字段定位原因 |
| HTTP 200 但 `data: null` | 通常为签名 / cookie 失效 |
