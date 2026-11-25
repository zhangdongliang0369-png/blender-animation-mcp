# Blender MCP V2 长期执行节点部署与验收

本文件是部署和验收草案。正式放行按 G01、G02、G03、G04 顺序签收。此前的临时线上记录用于复现实验参数，最终证据来自选定的长期主机和原 ChatGPT 对话。

## 部署基线

- 执行节点使用 Debian 13 x86-64 完整虚拟机或常开的独立 Linux 主机。初选 4 vCPU、8 GiB 内存、至少 80 GB SSD 和受限的管理员 SSH。共享 CPU 的渲染吞吐量需在 G02 实测。
- 复用 Railway 独立验收项目和现有 HTTPS 出站轮询协议。控制端和 worker 都固定到 `68456db63f63706b7fb84e9ec88d4fba24d28582`。在 G01 和 G02 通过前，正式服务、地址和 ChatGPT 插件沿用当前部署。
- Railway 控制端维持单副本和持久卷。`BLENDER_V2_DIR` 指向卷上的项目目录。控制端签名密钥、MCP 访问凭据和 worker 密钥各自保管。worker 通过 HTTPS 连到验收控制端，管理入口只开放给授权人员，worker 不监听业务公网端口。Blender 任务由原 Bubblewrap 边界切断网络。
- 当前 worker 和固定版控制端通过轮询和续租通信。记录此流量对 Railway 休眠和账单的影响。费用按部署侧分别记录，私有项目资产仅在授权环境传输。

## 安装并运行现有探针

在选定的 Linux 主机用管理员身份安装依赖。以下步骤使用普通的 Debian 非 setuid Bubblewrap，执行账户保持专用非 root。保留当前内核及服务安全限制，探针失败时停止接入并调查。

```sh
apt-get update
apt-get install -y bubblewrap ca-certificates curl ffmpeg git libegl1 \
  libegl-mesa0 libgl1-mesa-dri python3 python3-venv util-linux xz-utils

getent passwd blenderworker >/dev/null || \
  useradd --system --user-group --shell /usr/sbin/nologin blenderworker
install -d -m 0755 /opt/blender-mcp
install -d -m 0700 /etc/blender-mcp
git clone https://github.com/zhangdongliang0369-png/blender-animation-mcp.git /opt/blender-mcp/source
git -C /opt/blender-mcp/source checkout --detach 68456db63f63706b7fb84e9ec88d4fba24d28582
/usr/bin/python3 -m venv /opt/blender-mcp/venv
/opt/blender-mcp/venv/bin/python -m pip install --only-binary=:all: \
  -r /opt/blender-mcp/source/requirements.txt
```

在私有临时目录下载 Blender 官方 x86-64 二进制包，验证此前线上验收使用的 SHA-256 后安装到固定路径。代码、虚拟环境、Blender 安装目录和服务文件由管理员保管，`blenderworker` 无写权限。保留依赖版本清单和系统包版本供重建核对。

```sh
(
  set -e
  blender_archive_dir=$(mktemp -d)
  trap 'rm -f "$blender_archive_dir/blender-5.3.0-linux-x64.tar.xz"; rmdir "$blender_archive_dir"' EXIT
  cd "$blender_archive_dir"
  curl --fail --show-error --location --proto '=https' \
    --output blender-5.3.0-linux-x64.tar.xz \
    https://download.blender.org/release/Blender5.3/blender-5.3.0-linux-x64.tar.xz
  printf '%s\n' 'dacb8264197a3f7fda6001285a5a506fc97ee943881261c2c3bd63c882f22e05  blender-5.3.0-linux-x64.tar.xz' \
    | sha256sum --check
  tar -xJf blender-5.3.0-linux-x64.tar.xz -C /opt/blender-mcp --no-same-owner
  test -x /opt/blender-mcp/blender-5.3.0-linux-x64/blender
)
```

拒绝哈希不符的安装包。将本目录的服务模板和环境变量示例通过授权通道复制到主机。已有部署时先备份配置，保留原凭据。在 G01 使用同一份实际 unit、账户和限制，但暂用下面的 `ExecStart` 覆盖命令，使它只运行现有 `validate_node` 而不申领任务。安装模板时 `worker.env` 由 root 持有，权限 `0600`。G01 不需要给模板填写真实密钥。

```ini
# /etc/systemd/system/blender-mcp-worker.service.d/10-preflight-only.conf
[Service]
ExecStart=
ExecStart=/opt/blender-mcp/venv/bin/python -c 'import asyncio, os; from pathlib import Path; from v2.isolation import Sandbox; from v2.remote_worker import validate_node; root = Path(os.environ["BLENDER_REMOTE_WORKSPACE"]); root.mkdir(mode=0o700, parents=True, exist_ok=True); asyncio.run(validate_node(Sandbox(), root))'
```

```sh
install -m 0644 blender-mcp-worker.service /etc/systemd/system/blender-mcp-worker.service
test -f /etc/blender-mcp/worker.env || install -m 0600 worker.env.example /etc/blender-mcp/worker.env
systemd-analyze verify /etc/systemd/system/blender-mcp-worker.service
systemctl daemon-reload
systemctl start blender-mcp-worker.service
journalctl -u blender-mcp-worker.service -n 50 --no-pager
```

探针成功时日志含 `Worker runtime verified` 和 Blender 5.3.x 版本，进程成功退出。G01 全部证据保存并确认后，移除上述单一 G01 覆盖文件，填写实际验收端 HTTPS origin 和对应 worker 密钥，执行 `systemctl daemon-reload` 和 `systemctl enable --now blender-mcp-worker.service`。worker 每次启动会重复探针。验证进程非 root、unit 开机启用、无 worker 监听端口以及 systemd 的 cgroup 资源封顶。未通过的探针不得通过提权或禁用隔离放行。

通过 `systemctl show -p ControlGroup,MainPID,MemoryMax,TasksMax blender-mcp-worker.service` 定位服务自己的 cgroup。按该路径读取 `cpu.stat`、`memory.peak` 和 `pids.max`，同时记录 host CPU 核数及共享 CPU 的实际占用。保留现有探针日志及补测的用户、PID 和网络命名空间对照证据。对网络补测同时确认任务命名空间没有非回环接口及默认路由。

## 依次验收

| 关口 | 核对与通过证据 | 状态 |
| --- | --- | --- |
| G01 长期节点隔离 | 在最终 unit、用户和固定版 worker 上记录内核、系统包、实际 Blender 探针、用户／PID／网络命名空间、无任务网络、私密文件不可见、输入只读、任务输出版目录可写和资源限制。核对独立任务的内核文件大小、CPU 和地址空间限制，以及 systemd cgroup 的 CPU 可用量、内存和进程限额。用受控的小预算任务验证资源超限时终止并且没有可发布结果。 | 待实际节点 |
| G02 Railway 与长期节点 | 调用 `blender_info` 实际渲染探针。对已授权真实模型做签名上传、原件 SHA-256、导入、结构检查、审定操作、定点修改和签名下载，验证 PNG、MP4 和可编辑 `.blend`。用代表性片段验证 1080p 及目标帧率，记录帧数、耗时、CPU、峰值内存、磁盘和产物大小。必须完成下列长期运行和恢复演练，再做 V1 老工具回归。 | 等待 G01 |
| G03 正式插件 | G01、G02 签收后，在保留正式 MCP 地址和回滚基线的前提下按评审确认的发布配置接入正式端。检查正式插件发现 V2 新工具并完成实际调用，检查原有 `render_preview`、`preview_status` 和下载接口。核对正式控制端与 worker 的固定版本。 | 等待 G02 |
| G04 原对话 | 在用户指定的原业务对话中调用正式插件，以对话内授权的真实模型和已确认的制作要求完成制作与指定的局部修改。从该对话取得关键帧 PNG、MP4、可编辑 `.blend` 和前后两个工程版本，复核任务号、修改对象、原模型哈希和产物可打开性。 | 等待 G03 |

如包含机械装配，先在用户批准的 V1.1 资产资料中核准零件分组、轴线及坐标系、接触位置、运动范围、起止状态和时间点。缺少映射时暂停装配项目，留存待确认项。涉及镜头或材质的修改按对应的操作验收。机械装配按实际装配运动验收。

## 长期运行与恢复演练

在 G02 使用验收项目、独立卷和获授权资产。记录每次操作起止时间、任务及版本号和摘要化日志。

1. 运行空闲轮询和数次代表性任务，观察节点启停、Railway 运行状态、卷增长和实际账单。先以一个工作日作为观察安排，记录实际观察时长与样本，结果随证据提交。
2. 在真实任务运行时分别演练主机重启、仅中断 worker 到验收控制端的连接，以及 Railway 控制服务重启。网络演练保留管理员访问。让中断超过当前 45 秒租约期限，确认旧 worker 终止、迟到提交被拒且没有多生成版本。验证主机恢复后 worker 自动启动、重新探针并恢复轮询。核对两个端点的日志、旧工程哈希和重复提交行为。
3. 验证运行中取消、预期业务失败及 `retry_task`。重试使用新请求 ID，沿用原输入版本。核对旧失败原因保留、新任务状态正确、仅成功任务发布新版本。V1 回归使用正式旧工具和下载两个入口，并避免额外占用真实资产生产队列。
4. 演练备份与恢复。停领任务并确认没有活动写入，在同一维护窗口备份控制端持久卷上的 SQLite 目录、所有项目原件、工程版本及产物。若使用 Railway 卷快照，先查看恢复目标和实际计费条件。跨环境恢复使用经授权的加密归档。SQLite 使用一致性快照，或者在服务停止后连同整个目录和存在的 WAL 一起归档。备份对象和卷恢复点保持私有。另行保存固定源码版本、系统配置和密钥的恢复引用。[Railway 卷备份说明](https://docs.railway.com/volumes/backups)
5. 将备份恢复到独立的验收环境和空卷。重新配置隔离的测试访问凭据，运行 SQLite `PRAGMA integrity_check`，核对目录记录、已授权原件及两个版本的 SHA-256。实际下载、打开恢复的 `.blend` 再提交一项制作任务。记录备份间隔、恢复耗时、最后可恢复时间点和保存位置。配置正式备份周期和容量前按实际数据增长评估并确认费用。

## 回滚

在 G03 前复核正式 Railway 服务、原 MCP 地址、V1 对应变量引用以及已存在的 V1 回滚部署。当前 Git 基线为 `4118aa092b58b90e36725383ff90cd6bab9323a9`，Railway 回滚候选为 `355037a1-83bb-4f03-aeb0-bd8c0fd16e70`。如正式接入失败，停止新的 V2 接单，记录在途任务，恢复基线部署及关联配置，保留 V2 持久卷并取一致性备份。在原地址实测 V1 预演、查询和下载。隔离长期节点的问题时停用对应 worker，并通过受控配置撤销该 worker 的密钥。对照插件工具清单验证旧功能恢复。

正式验收报告在 G01–G04 全部通过后生成。归档实际参数、验收证据、部署环境与 Git 修订、时间、任务 ID、哈希、产物、资源消耗、备份恢复及回滚记录。生产日志、文件签名链接、密钥、私有模型和证据不进入公开仓库。
