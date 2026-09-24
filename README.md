# NexusGate-Sub

NexusGate 中转与节点控制面 + SubVault 独立订阅管理，在一台新服务器上共用一个 Caddy HTTPS 入口。这个仓库是**独立试验版**；原来的 [NexusGate](https://github.com/a2899882/NexusGate) 和 [dingyue/SubVault](https://github.com/a2899882/dingyue) 仓库无需改动。

## 运行方式

| 组件 | 对外路径 | 本机监听 | 数据与登录 | 用途 |
| --- | --- | --- | --- | --- |
| NexusGate | `https://域名/` | `127.0.0.1:8787` | `/var/lib/nexusgate`、统一管理员 | Agent、客户额度、转发与节点部署 |
| SubVault | NexusGate 侧栏；订阅链接仍在 `/vault/s/…` | `127.0.0.1:8790` | `/var/lib/nexusgate-subvault`、同一管理员会话 | 手动节点、独立订阅、模板、访问控制与日志 |
| Caddy | `80/443` | 公网 | 统一 TLS 证书与反代 | 一台机器只运行一份 Caddy |

NexusGate 侧栏的“独立订阅”区域嵌入 SubVault，管理时只需登录 NexusGate。旧的 `/vault/` 管理地址会跳回 NexusGate 的相应侧栏页面；已发给客户端的 `/vault/s/…` 订阅链接保持有效。两套客户、流量额度和节点记录**不会自动同步**，所以 NexusGate 中新增客户或节点不会出现在独立订阅中。SubVault 可以手动导入已有节点链接，但导入不等于接管 NexusGate 的节点凭据或计量。订阅下载限制无法替代节点侧用户隔离和双向流量上报。统一登录仍有两个后台进程和两份数据库，内存占用不会明显降低；公开订阅与流量上报在 NexusGate 控制面暂时不可用时仍可由 SubVault 处理。

## 新 VPS 一键安装

支持 Debian 12 / Ubuntu 22.04+，需要域名 A/AAAA 指向这台机器，以及开放 TCP 80/443。请在**新机器**用 root 执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/a2899882/NexusGate-Sub/main/scripts/install-combined.sh)
```

按提示输入域名和证书邮箱。安装器只输出 NexusGate 的管理员密码；请妥善保存。安装后打开 `https://你的域名/`，从左侧进入独立订阅。SubVault 的公开订阅链接仍带 `/vault/s/...`。新机器不应与旧机器同时使用相同域名服务生产客户端；测试请用独立 DNS 名称。**不要在现有的 NexusGate 或 dingyue 生产机上运行这个安装器。**

如果 NexusGate 安装成功但 SubVault 步骤失败，修好错误后在新机执行 `bash /opt/nexusgate/scripts/subvault-install.sh`，保留已生成的 NexusGate 管理员账号与两个数据库。

### 修复早期安装器的 Caddy 重复站点错误

早期试验版把反代配置的备份写入 `/etc/caddy/Caddyfile.d/`，Caddy 的通配导入会把该备份当作第二个同域名站点。新版 `ng update` 会移出遗留备份、校验 Caddy，并把已有 SubVault 数据接入统一登录；两个数据库和订阅 Token 不变。旧 SubVault 管理密码不再用于联合安装版。更新会重启 NexusGate，使原有管理员会话退出；请重新登录。不要重新运行仅用于新机器的联合安装命令。

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
ng account                        # 修改统一管理员账号和密码
```

已安装的试验机执行 `ng update`。如需直接使用仓库里的新版管理脚本升级，可执行 `bash <(curl -fsSL https://raw.githubusercontent.com/a2899882/NexusGate-Sub/main/scripts/nexusgate.sh) update`。统一登录后，SubVault “设置”中的账号按钮会打开 NexusGate 的账号设置；原 `ng sub-reset-password` 命令会提示改用 `ng account`。

恢复应在相同试验版完成安装的机器上进行。备份含管理员设置、桥接密钥、令牌与节点凭据，文件默认 0600，请放在安全位置。原 NexusGate 旧版备份可以恢复，但没有 SubVault 数据；SubVault 旧项目数据库也不会自动迁移。更新只替换源代码和 systemd 单元，不重置两个数据目录。SubVault 的访问日志、上报明细和非活跃绑定会按它原有清理策略定期删除。联合安装版不再把包含订阅 Token 的 URL 写入 Caddy 访问日志；升级后会删除旧版的受管访问日志及轮转文件，SubVault 服务日志也会遮盖订阅 Token 和二维码参数。面板 CPU 和内存随请求、节点规模以及日志量变化，安装器不承诺固定资源占用。

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
