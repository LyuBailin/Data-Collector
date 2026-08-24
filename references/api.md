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

## 端点清单

| 用途 | Method | Path | 关键 Query |
| --- | --- | --- | --- |
| 笔记搜索 | POST | `/api/sns/web/v1/search/notes` | `keyword`, `page`, `page_size`, `search_id`, `sort` |
| 用户搜索 | POST | `/api/sns/web/v1/search/users` | `keyword`, `page`, `page_size` |
| 热门榜 | POST | `/api/sns/web/v1/search/hotlist` | `category`, `page_size` |
| 首页 feed 推荐 | POST | `/api/sns/web/v1/feed` | `cursor_score`, `num` |
| 单篇笔记详情 | GET | `/api/sns/web/v1/feed` | `source_note_id={id}` |
| 用户基本信息 | GET | `/api/sns/web/v1/user/other_info` | `target_user_id` |
| 用户笔记列表 | GET | `/api/sns/web/v1/user/posted` | `user_id`, `cursor`, `num` |
| 用户收藏笔记 | GET | `/api/sns/web/v1/user/collect` | `user_id`, `cursor`, `num` |
| 笔记评论 | GET | `/api/sns/web/v1/comment/page` | `note_id`, `cursor`, `top_comment_id` |
| 子评论 | GET | `/api/sns/web/v1/comment/sub/page` | `note_id`, `root_comment_id`, `cursor` |
| 话题笔记列表 | POST | `/api/sns/web/v1/page_topic/notes` | `topic_id`, `page_size`, `cursor` |

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
