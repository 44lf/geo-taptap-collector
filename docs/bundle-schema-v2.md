# Bundle schema v2

Bundle schema v2 是跨平台 Python Collector 与 GEO Consumer 之间的多来源离线契约。它
支持 `refresh` 和 `discovery` job，保留归一化结果、有限原始证据和媒体字节，并以不可变
关联 ID 贯穿采集、传输和导入。

现有 `collector.ps1`、`run-collector.cmd` 和 `ops/windows-spike/` 继续生成并传输 v1
Bundle；v2 是新增 Python 路径，不改变 Spike 的运行时或 v1 importer 行为。

## 两层完整性边界

v2 有两个相关但不同的对象：

1. archive 内的 `manifest.json` 声明所有 payload 文件及逐文件 SHA-256；
2. Gateway transfer declaration（archive 外）声明固定对象键、archive 字节数和
   archive SHA-256。

archive 不能在自身内部可靠地声明自身最终 SHA-256，因此 archive 完整性属于外层
transfer descriptor。`BundleDescriptorV2` 将这两层一起表示，但外层 descriptor 不打包
进被散列的 archive。

## `manifest.json`

顶层字段：

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_version` | integer | 固定为 `2` |
| `correlations` | object | `collector_id`、`job_id`、`run_id`、`bundle_id`、`transport_id`，均不可变且非空 |
| `job` | object | `mode`、`destination`、`policy_version`、UTC `schedule_occurrence`、有序且唯一的 `source_order` |
| `created_at` | string | UTC ISO 8601 |
| `component_versions` | object | 至少记录 Collector 和 normalizer 契约版本；键唯一 |
| `limits` | object | 本 Bundle 实际采用的硬上限 |
| `items` | array | 有界结果项；每个 `item_key` 唯一 |
| `files` | array | 所有 payload 文件的完整清单 |

`job.mode` 为：

- `refresh`：每个 item 必须带由 Gateway 签发的 `target_game_id`，Consumer 以该目标为
  merge 锚点；
- `discovery`：item 可不带 `target_game_id`，Consumer 使用 GEO 自己的来源身份和归一化
  名称策略判断新建、合并或隔离。

一个 Bundle 可包含 `source_order` 中多个来源的结果，但每个 item 只属于一个来源。
来源固定为 `baidu`、`ninegame`、`yingyongbao`、`taptap`；未知来源必须在发起网络请求前
拒绝。

## Item 与证据

每个 item 包含：

```json
{
  "item_key": "target-101",
  "source": "baidu",
  "source_game_id": "baidu-101",
  "target_game_id": 101,
  "status": "success",
  "error_category": null,
  "http_status": 200,
  "stage": "detail",
  "attempt_count": 1,
  "normalized_path": "items/target-101/normalized.json",
  "raw_evidence_paths": ["items/target-101/raw/search.json"],
  "media_paths": ["items/target-101/media/icon.png"]
}
```

- `normalized_path` 是来源无关的 `CanonicalGameResult` JSON。
- `raw_evidence_paths` 只保存复现解析所需的有界响应证据；Cookie、XSRF、授权头、预签名
  URL 和生产凭据不得进入 Bundle。
- `media_paths` 保存已下载且待 Consumer 验证的图片。
- `success` 必须声明 `source_game_id` 和 `normalized_path`；`miss`、`blocked`、`error`
  可仅声明原始证据。
- `blocked` 必须使用 `error_category=blocked`；普通 `error` 必须明确
  `configuration|permanent|retryable|security`。`success` 和 `miss` 不得伪造失败分类。
- `http_status`、`stage` 和正数 `attempt_count`用于审计有界重试；它们不能包含 URL 查询、
  请求头或响应正文。

## 完整文件清单

`files` 中每一项包含：

```json
{
  "path": "items/target-101/raw/search.json",
  "role": "raw_evidence",
  "media_type": "application/json",
  "size_bytes": 500,
  "sha256": "<64 lowercase hex>",
  "source": "baidu",
  "source_game_id": "baidu-101",
  "source_url": null
}
```

`role` 为 `normalized`、`raw_evidence` 或 `media`。媒体项必须带 `source_url`。路径必须是
规范 POSIX 相对路径，禁止绝对路径、反斜杠、`.`、`..` 和重复路径。每个 item 引用的
文件必须存在于清单且 role/source 一致；清单中也不得存在未被任何 item 引用的 payload
文件。

## 有界限制

`limits` 必须包含并由 Collector 与 Consumer 双向执行：

- `max_items`
- `max_requests`
- `max_files`
- `max_file_bytes`
- `max_total_bytes`
- `max_raw_evidence_bytes`
- `max_media_files_per_item`

Gateway job manifest 另外限制 target/discovery path、请求次数、媒体数量与媒体总字节数。
Consumer 使用服务端允许值和 Bundle 声明值中更严格的一方；超限在任何业务写入前作为
永久 schema/unsafe-content 错误隔离。

## 外层 archive descriptor

```json
{
  "object_key": "incoming/collector-local-01/transport-01.tar.zst",
  "size_bytes": 2048,
  "sha256": "<64 lowercase hex>"
}
```

同一 `transport_id` 在整个生命周期内只能对应一个 destination、object key、字节数和
archive SHA-256。重试复用相同身份和 archive，不重新采集。

## v1 兼容说明

v1 仍是 Windows PowerShell Spike 的受支持历史契约，详见
[`bundle-schema-v1.md`](bundle-schema-v1.md)。v1 是单来源 TapTap、目录式交付，且缺少
v2 的完整关联 ID、job mode、多来源 item、组件版本和外层不可变 archive identity。

v2 Consumer 必须按 `schema_version` 显式分派；不得把 v1 静默解释为 v2，也不得要求
现有 v1 Spike 生成 v2。v1 的移除需要单独迁移、观察期和授权，不属于本 foundation
切片。
