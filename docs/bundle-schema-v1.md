# Bundle schema v1

Bundle 是一个目录，不是单个 JSON 文件。消费者必须先验证整个目录，再执行任何数据库操作。

## `manifest.json`

顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | integer | 固定为 `1` |
| `bundle_id` | string | 本轮稳定标识 |
| `collector_version` | string | Collector 版本 |
| `source` | string | 固定为 `taptap` |
| `started_at` | string | UTC ISO 8601 |
| `finished_at` | string | UTC ISO 8601 |
| `targets` | array | 1–20 个目标结果 |
| `files` | array | 所有 game JSON 和图片的完整性清单 |

成功目标包含：

```json
{
  "target_game_id": 101,
  "name": "原神",
  "category_id": 501,
  "status": "success",
  "game_file": "games/101/game.json",
  "asset_files": [
    "games/101/images/icon.png",
    "games/101/images/screenshot-01.jpg"
  ]
}
```

`miss`、`error` 或 `blocked` 目标不得声明 `game_file`。失败证据记录在 `report.json`。

## 文件清单

JSON 文件：

```json
{
  "path": "games/101/game.json",
  "bytes": 3200,
  "sha256": "<64 lowercase hex>",
  "media_type": "application/json",
  "role": "game"
}
```

图片文件额外包含来源 URL：

```json
{
  "path": "games/101/images/icon.png",
  "bytes": 219478,
  "sha256": "<64 lowercase hex>",
  "media_type": "image/png",
  "role": "icon",
  "source_url": "https://..."
}
```

合法 MIME 类型仅包括：

- `application/json`
- `image/png`
- `image/jpeg`

## 消费者校验要求

消费者在数据库写入前必须验证：

1. schema 版本和必填字段；
2. 每个相对路径位于 bundle 根目录内；
3. 文件数量、单文件大小和全包大小上限；
4. 清单字节数等于实际文件大小；
5. SHA-256 一致；
6. PNG、JPEG 和 JSON 内容签名正确；
7. 目标 ID、文件路径不重复；
8. 每个清单文件都被目标引用，且不存在未声明文件。

GEO 当前 importer 接受 schema v1，并通过 `pre_downloaded` /
`pre_downloaded_icon` 将已验证字节交给现有游戏库和图片库。
