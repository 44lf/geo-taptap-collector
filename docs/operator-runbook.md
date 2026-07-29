# Python Collector Phase 1 运维手册

## 1. 边界

Python Collector 在本地电脑或 Linux 容器中运行四个已验证来源适配器，通过 HTTPS
Gateway 领取不可变 job。Bundle 只上传到 Gateway 签发的短时 Inbox URL；本机不保存
GEO MySQL、业务 MinIO 或生产 SSH 凭据。GEO Consumer 才负责校验、归一化和业务写入。

Phase 1 不启用生产，不要求 Kubernetes，也不再运行 `ops/windows-spike` 的点击式脚本。
旧 PowerShell v1 Spike 只保留作历史诊断和回滚证据。

## 2. 安装和持久目录

需要 Python 3.11+。在版本目录中安装：

```powershell
python -m pip install .
geo-game-collector --version
```

默认持久目录：

- Windows：`%LOCALAPPDATA%\GEOGameCollector`
- Linux：`$XDG_DATA_HOME/geo-game-collector`，未设置时为
  `~/.local/share/geo-game-collector`

目录固定分为 `config/`、`jobs/`、`spool/`、`state/`、`logs/` 和 `staging/`。
升级应用代码时不得删除这些目录。

## 3. 身份配置

公开配置不允许出现 secret、password、credential、token 或 Cookie：

```powershell
geo-game-collector configure `
  --gateway-url https://geo.example.com/ `
  --collector-id collector-local-1 `
  --data-dir D:\GEOGameCollector
```

将运维签发的可撤销 Collector credential 存入 Windows Credential Manager、macOS
Keychain 或 Linux keyring：

```powershell
geo-game-collector credential set
geo-game-collector credential status
```

`credential set` 使用隐藏输入，不能把 secret 放在命令行参数或 JSON 配置中。容器使用
`GEO_COLLECTOR_CREDENTIAL_FILE` 指向只读 secret 文件。环境变量
`GEO_COLLECTOR_CREDENTIAL` 仅作受控临时兼容入口。

## 4. 首次诊断和手动运行

```powershell
geo-game-collector doctor
geo-game-collector run-now --mode refresh
geo-game-collector run-now --mode discovery
```

`doctor` 会刷新 Gateway 配置并检查凭据、配置时效、持久目录和 spool 高水位。手动运行
与后台调度使用同一个 `CollectorAgent`：领取 job 后先落盘，再爬取、逐 item 原子
checkpoint、构建 Bundle、登记 run、进入 spool 并传输。

不得重复手工指定不同 claim ID 来制造同一时段的重复采集。故障重试使用原 job、
run、bundle、transport 身份，不重新爬取。

## 5. 后台运行和停止

前台观察：

```powershell
geo-game-collector agent
```

后台进程从 GEO 配置读取 refresh/discovery 窗口，本地持久化 `next_due_at`，离线恢复时只
执行时效内且不超过上限的 occurrence。每个 occurrence 的 claim ID 由模式和 UTC 时间
确定，因此重启重放保持幂等。

Windows 可由 Task Scheduler 在登录或开机时启动同一条 `agent` 命令；Linux 可使用
systemd 或 Compose。不要为每个来源创建独立定时任务，也不要同时启用 GEO
`in_process` 与外部 Collector 调度。

`Ctrl+C`、SIGINT 和 SIGTERM 会请求干净停止；当前原子文件操作结束后退出，已进入
spool 的 Bundle 不会删除。

## 6. 日志、Web 和健康检查

- 本地技术日志：`logs/active.jsonl` 与轮转 segment，UTC、JSON、带
  collector/run/job/bundle/transport/source 关联字段。
- GEO Web：管理员的“Collector 管理”页展示节点、最近运行、事件时间线、积压、传输
  receipt 和逐 item 导入结果。
- 日志和事件在持久化前移除 Bearer、Cookie/XSRF、数据库 URI 凭据、预签名查询参数及
  raw body。原始响应属于 Bundle evidence，不属于日志。

命令：

```powershell
geo-game-collector health
geo-game-collector readiness
geo-game-collector metrics
```

`health` 只检查进程可写持久状态；`readiness` 还要求凭据可用、配置未过期且 spool 未达
高水位；`metrics` 输出 Prometheus 文本格式的 entries、pending、terminal 和 bytes。

## 7. Spool 检查与网络恢复

```powershell
geo-game-collector drain
```

`spool/transfers/<transport_id>/` 是收到匹配 `PROCESSED` receipt 前的本地事实源。
archive 每次发送前校验 size 和 SHA-256。超时、连接失败、429/5xx 使用有界指数退避；
401/403、身份/哈希/schema 冲突立即停止自动重试并保留证据。半写入目录会移动到
`spool/quarantine/`，不会静默覆盖。

禁止手工删除 pending、retry_wait、blocked 或 failed 目录。只有匹配 transport ID 与
archive SHA-256 的 processed receipt 且超过保留宽限期后，才可按清理策略处理。

## 8. Docker / Compose

```bash
mkdir -p secrets
printf '%s' 'credential-id:secret' > secrets/collector-credential.txt
docker compose -f compose.example.yaml config
docker compose -f compose.example.yaml up --build
```

镜像使用 UID/GID 10001 非 root 用户、只读根文件系统、移除 Linux capabilities，并把
唯一可变状态放在 `collector-data` volume。Docker 只是可选载体；本地原生运行仍是
Phase 1 默认方式。

## 9. 安全升级、回滚和凭据轮换

升级：

1. 停止 Agent，确认进程已退出。
2. 备份公开配置；保留整个持久数据目录。
3. 安装新版本，运行 `--version`、`health`、`doctor`。
4. 先执行 `drain`，再启动 `agent`。

回滚只切换应用版本，不回滚或删除 `jobs/`、`spool/`、`state/`。Bundle schema 或状态
格式不兼容时停止并由工程人员迁移，不能用旧脚本覆盖。

凭据轮换：

1. Gateway 创建/启用新 credential。
2. 本地执行 `credential set` 覆盖密钥库值。
3. 执行 `doctor` 和一次 `drain`。
4. 确认证据后在 Gateway 撤销旧 credential。

## 10. 卸载

先停止 Agent，再卸载 Python 包或删除容器。执行 `credential delete` 可移除 OS 密钥库
条目。默认不删除持久数据目录；其中可能有尚未确认的 Bundle。只有运维确认
spool pending 为 0、所有 receipt 已核对且保留期满足后，才能另行归档或删除数据。
