# AGENTS.md

本仓库是独立 Windows TapTap Collector。开发、运行、安全边界和测试命令集中维护在
[`README.md`](./README.md)。

修改约束：

- `collector.ps1` 必须保持 ASCII-only，兼容 Windows PowerShell 5.1。
- 现有 `collector.ps1` / Windows Spike 不得新增 Python 依赖；新的跨平台 Collector Agent
  以 Python 为运行时。两条路径都不得依赖 GEO 源码或生产凭据。
- 不得移除 `403/405/429` 立即停止整轮任务的保护。
- bundle schema 变更必须升级 `schema_version`，同步更新文档、测试与 GEO importer。
- 不提交 `targets.json`、`output/`、运行日志或 Cookie/XSRF token。
