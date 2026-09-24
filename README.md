# NexusGate-Sub

NexusGate 中转与节点控制面 + SubVault 独立订阅管理，在一台新服务器上共用一个 Caddy HTTPS 入口。这个仓库是**独立试验版**；原来的 [NexusGate](https://github.com/a2899882/NexusGate) 和 [dingyue/SubVault](https://github.com/a2899882/dingyue) 仓库无需改动。

## 运行方式

| 组件 | 对外路径 | 本机监听 | 数据与登录 | 用途 |
| --- | --- | --- | --- | --- |
| NexusGate | `https://域名/` | `127.0.0.1:8787` | `/var/lib/nexusgate`、独立管理员 | Agent、客户额度、转发与节点部署 |
| SubVault | `https://域名/vault/` | `127.0.0.1:8790` | `/var/lib/nexusgate-subvault`、独立管理员 | 手动节点、独立订阅、模板、访问控制与日志 |
| Caddy | `80/443` | 公网 | 统一 TLS 证书与反代 | 一台机器只运行一份 Caddy |

NexusGate 侧栏的“独立订阅”区域嵌入 SubVault；首次进入需要 SubVault 自己的账号。两套客户、流量额度和节点记录**不会自动同步**。SubVault 可以手动导入已有节点链接，但导入不等于接管 NexusGate 的节点凭据或计量。订阅下载限制无法替代节点侧用户隔离和双向流量上报。

## 新 VPS 一键安装

支持 Debian 12 / Ubuntu 22.04+，需要域名 A/AAAA 指向这台机器，以及开放 TCP 80/443。请在**新机器**用 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/a2899882/NexusGate-Sub/main/scripts/install-combined.sh)
```

按提示输入域名和证书邮箱。安装器会生成**两组**管理员密码，分别在 NexusGate 与 SubVault 步骤输出；请妥善保存。安装后打开 `https://你的域名/`，从左侧进入独立订阅；也可直达 `https://你的域名/vault/`。SubVault 的公开订阅链接会自动带 `/vault/s/...`。新机器不应与旧机器同时使用相同域名服务生产客户端；测试请用独立 DNS 名称。**不要在现有的 NexusGate 或 dingyue 生产机上运行这个安装器。**

如果 NexusGate 安装成功但 SubVault 步骤失败，修好错误后在新机执行 `bash /opt/nexusgate/scripts/subvault-install.sh`，保留已生成的后台密码与数据库。

### 修复早期安装器的 Caddy 重复站点错误

早期试验版把反代配置的备份写入 `/etc/caddy/Caddyfile.d/`，Caddy 的通配导入会把该备份当作第二个同域名站点。已安装的机器运行 `ng update && bash /opt/nexusgate/scripts/subvault-install.sh`：第一步取得新版安装器和联合备份，第二步把遗留备份移到 `/etc/caddy/nexusgate-backups/`，校验并重新加载反代配置，保留两个数据库。**第一次升级仍由旧版 `ng` 脚本执行，所以不能省略第二步；往后的升级会自动对账反代配置。** 如果首次失败时没有收到 SubVault 密码，运行 `ng sub-reset-password` 生成新密码。不要重新运行仅用于新机器的联合安装命令。

## 运维

```bash
ng status                         # NexusGate 状态
systemctl status nexusgate-subvault # 独立订阅状态
ng update                         # 同仓库升级两个服务，先做联合备份
ng backup                         # 两个数据库和环境配置的迁移包
ng restore /root/备份文件.tar.gz   # 恢复联合备份
ng domain new.example.com         # 同步域名和 SubVault 公开链接
ng cert                           # 检查 Caddy 证书
ng sub-info                       # 查看独立订阅地址、服务与磁盘空间
ng sub-logs                       # 查看独立订阅日志
ng sub-compact                    # 联合备份后压缩订阅数据库
ng sub-reset-password             # 重置独立订阅密码并使旧会话失效
```

恢复应在相同试验版完成安装的机器上进行。备份含管理员设置、令牌与节点凭据，文件默认 0600，请放在安全位置。原 NexusGate 旧版备份可以恢复，但没有 SubVault 数据；SubVault 旧项目数据库也不会自动迁移。更新只替换源代码和 systemd 单元，不重置两个数据目录。SubVault 的访问日志、上报明细和非活跃绑定会按它原有清理策略定期删除；Caddy 日志轮转由 NexusGate 安装器设置。面板 CPU 和内存随请求、节点规模以及日志量变化，安装器不承诺固定资源占用。

### 从已有 dingyue 搬迁数据（可选）

先给旧实例停写并备份；只将其 SQLite 数据库复制到新机器的 `/var/lib/nexusgate-subvault/subvault.db`，保留新机 `/etc/nexusgate-subvault.env` 中的站点域名、管理员启动密钥与上报密钥配置，或按原实例的密钥迁移。复制前停止 `nexusgate-subvault` 服务，复制后设置 `nexusgate-subvault:nexusgate-subvault` 所有权，再启动服务。数据库及其 WAL 文件必须来自一致的离线副本。此操作会覆盖试验机上的 SubVault 数据；先运行 `ng backup`。若旧库中的管理员密码哈希与新环境不同，使用原项目的管理员恢复流程。NexusGate 数据无需迁移即可空白测试。

### 单机部署冲突

原 dingyue 的 Docker Compose 和 NexusGate 的安装器都会配置 80/443，直接逐个安装在一台机器会端口冲突，也会产生两套 Caddy。合并安装器只部署一份 Caddy，并把两个后台服务绑定到 loopback；面板端口不向公网开放。HTTPS 证书仍由 Caddy 自动申请和续签。入口/中转/落地 Agent 与这两个面板服务无端口冲突，它们使用各自的节点端口范围；同机部署节点前请检查节点端口、防火墙和 CPU/带宽余量。

## 开发测试

```bash
npm ci --ignore-scripts
npm run check
npm test
PYTHONPATH=subvault python3 -m unittest discover -s subvault/tests -v
```

原 NexusGate 说明见 [docs/NEXUSGATE_ORIGINAL.md](docs/NEXUSGATE_ORIGINAL.md)，原 SubVault 功能与 API 说明见 [subvault/README.md](subvault/README.md)。原文中的独立安装命令属于各自原项目，**本试验版请使用上面的联合安装器**。NexusGate 和 SubVault 均采用 MIT 协议；各自许可证保存在仓库根目录与 `subvault/LICENSE`。
