# GEO Game Collector

当前主线是跨平台 Python Collector：在本地电脑运行百度、九游、应用宝、TapTap
适配器，通过 GEO Gateway 领取 job、生成 Bundle schema v2、进入持久 spool，并由
GEO Consumer 归一化后写入业务 MySQL/MinIO。它不直连生产数据库或业务对象存储。

```text
Local Python Collector
  -> HTTPS Gateway + short-lived Inbox upload
  -> durable transfer queue
  -> GEO Consumer
  -> existing GEO normalization/upsert/image services
```

安装和运行：

```powershell
python -m pip install .
geo-game-collector configure --gateway-url https://geo.example.com/ --collector-id collector-local-1
geo-game-collector credential set
geo-game-collector doctor
geo-game-collector run-now --mode refresh
geo-game-collector agent
```

完整安装、日志、spool、升级、回滚、凭据轮换、Docker 和卸载流程见
[Python Collector Phase 1 运维手册](docs/operator-runbook.md)。

## Legacy Windows TapTap Spike

面向 Windows Server 的独立 TapTap 游戏资料采集器。它读取一份最多 20 个游戏的显式
目标清单，采集游戏元数据、图标和截图，并生成带 SHA-256 完整性清单的离线 bundle，
供 GEO 生产侧校验和幂等导入。

> 仅用于已获得授权的数据采集。禁止用于代理轮换、指纹伪装、绕过访问控制或规避限流。
> Collector 遇到 HTTP `403`、`405` 或 `429` 会立即停止整轮任务。

## 为什么独立仓库

- Legacy Windows Spike 不需要 GEO 源码、Python、MySQL 或 MinIO 凭据。
- 生产 GEO 不实时依赖公司网络，只消费版本化 bundle。
- TapTap 网络协议变化和 Windows 运行环境可以独立发布、回滚。
- 生产自动游戏来源仍由 GEO 自己管理，本仓库不修改生产 scheduler。

```text
GEO 生产导出 targets.json
            │
            ▼
Windows Collector
            │ bundle schema v1
            ▼
GEO bundle importer → MySQL + MinIO
```

## 环境要求

- Windows PowerShell 5.1 或更高版本。
- 能直接访问 TapTap HTTPS 服务和图片 CDN。
- 现有 Windows TapTap Spike 不需要安装 Python。
- 操作者已确认组织与 TapTap 的授权边界。

跨平台 Python Collector 是当前工程化主线；`collector.ps1`、`run-collector.cmd` 和
`ops/windows-spike/` 仅作为 legacy Spike 保留，不再作为日常运行入口。

## 快速开始

1. 将 `targets.example.json` 复制为 `targets.json`。
2. 使用 GEO 生产导出的真实 `target_game_id`、`category_id` 和游戏名称替换示例。
3. 双击 `run-collector.cmd`。
4. 在 `output/<bundle_id>/report.json` 检查状态。
5. 将整个 bundle 目录交给 GEO importer；不要只复制 `manifest.json`。

目标清单示例：

```json
{
  "schema_version": 1,
  "bundle_id": "taptap-stability-20260728",
  "max_screenshots": 3,
  "delay_seconds": 30,
  "targets": [
    {
      "target_game_id": 101,
      "name": "原神",
      "category_id": 501,
      "icon_local": false
    }
  ]
}
```

约束：

- `targets` 必须包含 1–20 个唯一目标。
- `target_game_id` 是 GEO 生产数据库的游戏行 ID，不是 TapTap 游戏 ID。
- 搜索只接受标题完全相同的 `brand` 结果，不使用模糊命中。
- `max_screenshots` 范围是 0–6。
- `delay_seconds` 范围是 0–3600；稳定性测试建议 30 秒。
- 相同 `bundle_id` 重跑会跳过 checkpoint 中已成功的目标。

## 输出

```text
output/<bundle_id>/
├── checkpoint.json
├── manifest.json
├── report.json
└── games/<target_game_id>/
    ├── game.json
    └── images/
        ├── icon.png
        └── screenshot-01.jpg
```

`manifest.json` 使用 bundle schema v1。每个文件记录：

- POSIX 风格相对路径；
- 字节数；
- SHA-256；
- MIME 类型；
- 角色（`game`、`icon`、`screenshot`）；
- 图片来源 URL。

详细契约见 [docs/bundle-schema-v1.md](docs/bundle-schema-v1.md)。

新增 Python Collector 使用 Bundle schema v2；其多来源、原始/归一化证据、完整文件
清单、组件版本、archive 完整性和有界限制契约见
[docs/bundle-schema-v2.md](docs/bundle-schema-v2.md)。现有 PowerShell Spike 继续使用
v1。

Python Collector 的本地 spool 会把 archive 复制到自身数据目录并重新校验字节数与
SHA-256。`transport_id`、对象键和 archive 身份一旦建立即不可变；网络失败只递增持久化
尝试状态，不重新采集或重新打包。只有收到 `transport_id` 与 archive SHA-256 都匹配的
`PROCESSED` receipt，且超过保留宽限期，文件才会成为可清理候选；失败 receipt 和未确认
archive 会继续保留。spool 达到数量或字节高水位时拒绝接收新 Bundle，不丢弃积压。

`report.json` 记录公网出口 IP、每阶段 HTTP 状态与耗时、目标结果和阻断状态，但不会保存
Cookie、XSRF token 或生产凭据。

## 退出码

- `0`：本轮没有失败或阻断。
- `1`：manifest 或启动参数无效。
- `2`：至少一个普通目标失败，但未遇到阻断状态。
- `3`：遇到 `403/405/429`，整轮已停止。

## 断点续跑

Collector 每成功完成一个目标就更新 `checkpoint.json`。相同 `bundle_id` 重跑时：

- 成功目标直接跳过；
- miss 和普通 error 目标重新尝试；
- 发生阻断时不继续后续目标。

若需要重新做一轮独立稳定性测试，请使用新的 `bundle_id`。

## 开发验证

运行时没有第三方依赖。开发测试需要 Windows、Python 和 pytest：

```powershell
python -m pip install -r requirements-dev.txt
pytest -q
```

Python Collector foundation 的可编辑安装及质量门禁：

```powershell
python -m pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy
```

测试通过真实 `powershell.exe` 执行离线 fixture，覆盖：

- 20 游戏上限；
- 精确标题匹配；
- `403/405/429` 阻断停机；
- checkpoint 断点续跑；
- bundle 文件清单和 SHA-256；
- 输出不泄露会话 secret。

## 安全边界

- 本仓库不得出现生产数据库、MinIO、SSH 或 API 凭据。
- 不提交 `targets.json`、`output/` 或运行日志。
- 不开放入站服务；传输由操作者复制 bundle 或后续使用短时预签名对象存储 URL。
- GEO importer 必须先完整 dry-run 校验，再由生产操作者决定是否正式导入。
