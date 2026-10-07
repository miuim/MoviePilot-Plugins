# ClipNotes 同步协议 v1

供 Obsidian 插件（客户端）与 MoviePilot ClipNotes 插件（服务端）之间的增量同步使用。

- 协议标识：`clipnotes-sync/1`
- 服务端版本：ClipNotes v1.0.2
- 传输：HTTP + JSON，UTF-8
- 设计目标：**幂等、可断点续传、内容变更可感知**

---

## 1. 端点与鉴权

基址（两个前缀等价，`v2` 为镜像注册）：

```
/api/v1/plugin/ClipNotes
/api/v2/plugin/ClipNotes
```

鉴权：`apikey`，值为 MoviePilot 的 `API_TOKEN`，两种传法：

```
GET /notes?apikey=<API_TOKEN>
GET /notes        Header: X-API-KEY: <API_TOKEN>
```

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/notes` | 列表 / 增量同步（游标） |
| GET | `/notes/{content_id}` | 单条详情（Markdown + 元数据 + 资源清单） |
| GET | `/assets/{hash}` | 下载图片等资源文件 |
| POST | `/notes/{content_id}/ack` | 同步确认（写回状态） |
| POST | `/notes` | 提交新链接（`{"url": "..."}`） |
| GET | `/stats` | 统计与协议信息 |

---

## 2. 增量同步循环

客户端只需持久化一个东西：**上次成功处理到的 `next_cursor`**。其余都是可推导的。

```text
cursor = 本地保存的 cursor（首次为空）
loop:
    resp = GET /notes?status=ready,synced&since={cursor}&limit=20&include_deleted=1
    for item in resp.items:
        if item.content_hash == 本地该 content_id 的 content_hash:
            跳过（内容未变，例如只发生了状态流转）
        else:
            detail = GET /notes/{item.content_id}
            写 Markdown 到 vault/{detail.filename}.md
            for asset in detail.assets:
                GET asset.api_path → 保存到本地附件目录
            重写正文中的图片引用为本地路径
        POST /notes/{item.content_id}/ack
             body: {"content_hash": item.content_hash, "revision": item.revision, "note": "本地相对路径"}
        # ack 返回 stale=true 表示刚才同步的是旧版本，本条不推进，下轮会重新拉到
    for tomb in resp.get("deleted", []):
        删除本地对应文件
    if resp.has_more: continue
    cursor = resp.next_cursor      # 只在整页处理成功后提交
    sleep
```

要点：

1. **游标是 keyset 而非 offset**：排序键是 `(updated_at, content_id)`，按升序返回。分页过程中新写入的记录不会被跳过，也不会因为插入导致重复。
2. **先落盘再 ack**：某条写入失败时不要推进游标，下一轮会重新拉到同一条，因为服务端不关心客户端进度。
3. **内容去重靠 `content_hash`**：服务端的状态流转（`received → parsing → ai_pending → ready`）会更新 `updated_at`，但内容一致时 `content_hash` 不变，客户端可用它跳过重复写入。
4. **`next_cursor` 是空页时原样返回**：没有新内容时不会丢失游标，可以安全继续轮询。

---

## 3. 游标语义

- 格式：`v1|<updated_at>|<content_id>`，例如 `v1|2026-10-07 02:40:11|9d2ea243832fb7e2`
- **客户端必须把游标当作不透明字符串原样回传**，不要解析、不要拼接、不要跨版本缓存。
- 兼容：`since` 也接受纯时间戳（如 `2026-10-07 02:00:00`），便于手工调试；此时不设 `content_id` 次级条件。
- 排序为**升序**，因此游标天然单调推进，无需担心时钟回拨造成的漏读（时间相同的记录由 `content_id` 兜底排序）。

---

## 4. `GET /notes` 请求参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `status` | string | 状态过滤，逗号分隔：`received,parsing,ai_pending,ready,synced,failed` |
| `platform` | string | 平台过滤，逗号分隔：`wechat,xiaohongshu,generic` |
| `since` | string | 同步游标（见上）。与 `limit` 任一存在即进入游标模式 |
| `limit` | int | 游标模式每页条数，默认 20，上限 100 |
| `include_deleted` | bool | 游标模式下附带删除墓碑 |
| `page` / `count` | int | 浏览模式（偏移分页，按 `created_at` 倒序） |

> 建议同步端固定使用 `status=ready,synced`；如果想感知失败项，可另起一轮拉 `status=failed`。

### 响应（游标模式）

```json
{
  "success": true,
  "protocol": "clipnotes-sync/1",
  "mode": "cursor",
  "server_time": "2026-10-07 02:41:03",
  "count": 2,
  "remaining": 2,
  "has_more": false,
  "next_cursor": "v1|2026-10-07 02:40:11|9d2ea243832fb7e2",
  "items": [
    {
      "content_id": "9d2ea243832fb7e2",
      "source_url": "https://www.xiaohongshu.com/discovery/item/6aa6c22e...",
      "platform": "xiaohongshu",
      "platform_label": "小红书",
      "status": "ready",
      "title": "Obsidian新手入门安装配置与核心操作经验分享",
      "category": "Obsidian",
      "tags": ["Obsidian", "新手入门"],
      "created_at": "2026-10-07 01:54:17",
      "updated_at": "2026-10-07 02:40:11",
      "revision": 37,
      "content_hash": "a1b2c3d4e5f60718",
      "markdown_size": 2536,
      "assets_count": 6,
      "error": ""
    }
  ],
  "deleted": [
    {"content_id": "8286ed93cb94a8af", "deleted_at": "2026-10-07 02:12:00"}
  ]
}
```

字段说明：

- `remaining`：游标之后仍待拉取的条数（含本页），客户端可据此显示进度。
- `has_more`：为 `true` 时应立即用 `next_cursor` 继续拉取。
- `content_hash`：Markdown 内容的 SHA-256 前 16 位，作为文件级 ETag。
- `revision`：服务端单调递增版本号，仅用于调试比对，客户端**不要**用它排序。
- `server_time`：服务端当前时间，客户端不要依赖本地时钟。

### 响应（浏览模式）

保留 `mode=offset` + `page` / `total`，不返回有效 `next_cursor`，用于人工排查与调试。

---

## 5. `GET /notes/{content_id}` 响应

在列表项字段之外，额外返回：

| 字段 | 说明 |
|---|---|
| `filename` | 建议的 Markdown 文件名（已过滤非法字符、截断到 80 字符），**不含扩展名** |
| `markdown` | 完整 Markdown 正文（含 YAML frontmatter） |
| `meta.author` / `meta.publish_time` / `meta.video` / `meta.images` / `meta.extra` | 原始抓取信息 |
| `assets[]` | 资源清单，每项含 `hash`、`source_url`、`filename`、`downloaded`、`size`、`api_path` |
| `synced_at` | 上次 ack 时间 |

**重要**：Markdown 正文里的图片引用仍是原始 CDN 地址（会过期）。客户端必须用 `assets[].hash` 对应的本地文件替换这些引用；`assets[].source_url` 可用于匹配替换位置（注意小红书 CDN 地址每次抓取都会变化，匹配时应以路径末段的内容标识为准）。

---

## 6. `POST /notes/{content_id}/ack`

请求体（字段全部可选）：

```json
{ "status": "synced", "note": "知识/Obsidian新手入门.md", "revision": 37, "content_hash": "a1b2c3d4e5f60718" }
```

- 不带 `content_hash`：直接标记为 `synced`（并发写 `synced_at`）。
- 带 `content_hash` 且与服务端不一致：返回 `stale: true`，**不修改任何状态**，客户端应重新拉取该条。这是防止"同步了旧版本却标记完成"的保护。
- `status` 可传 `ready` 用于把条目退回待同步队列（例如本地文件被误删）。

响应：

```json
{ "success": true, "stale": false, "content_id": "9d2ea243832fb7e2",
  "status": "synced", "synced_at": "2026-10-07 02:42:10",
  "revision": 38, "content_hash": "a1b2c3d4e5f60718" }
```

**幂等性**：重复 ack 同一条是安全的；若条目已是 `synced`，再次 ack 只会刷新 `synced_at`。

---

## 7. 错误与重试

| HTTP | 场景 | 客户端处理 |
|---|---|---|
| 401 | `apikey` 缺失或错误 | 检查配置，不要重试 |
| 404 | 笔记或资源不存在（可能已被删除） | 记日志，跳过该条 |
| 400 | 参数非法（链接无效、状态非法） | 不要重试 |

网络错误一律**不推进游标**并退避重试，服务端接口全部幂等。

---

## 8. 已知限制

1. **删除墓碑仅保留最近 200 条**，超过后最早的墓碑会被清理；客户端长期离线超过该窗口时无法感知删除。
2. **状态流转会产生新的 `updated_at`**：客户端若只按时间比较而不比较 `content_hash`，会做无谓的重复下载。
3. **`assets` 只保留正文图片**（每篇上限 20 张），视频只记录地址不落盘。
4. **`xsec_token` 会过期**：小红书笔记入库后长期保存，重新解析旧链接可能失败；如需重跑建议重新提交分享链接（`content_id` 相同，不会产生新记录）。
5. 协议为**只读 + ack**：客户端不接受反向写入内容，Markdown 由服务端生成。

---

## 9. 版本与兼容

- 响应中的 `protocol` 字段标识协议版本；游标自带 `v1` 前缀。
- 新增字段属于向后兼容变更，客户端应忽略未知字段；删除字段或改变语义时会提升协议版本号。
