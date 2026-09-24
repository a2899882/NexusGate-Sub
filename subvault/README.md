# SubVault（dingyue）

轻量、自托管的私人代理订阅管理面板。面向 Debian 12 小型 VPS，提供中文管理界面、独立订阅 Token、节点分组、常用客户端模板、到期/流量/IP/设备策略和访问审计。

> 合法使用：本项目只负责管理你有权使用的节点与订阅。请遵守所在地区法律、服务商条款与网络使用政策。

## 功能

- 节点单条添加和批量导入：Shadowsocks、VMess、VLESS、Trojan、Hysteria2、TUIC
- 节点和订阅支持服务端搜索、分组/状态筛选、分页、每页数量与批量启停/移动/删除；数百条记录也无需一次加载
- 创建订阅时可按节点名称和分组筛选，批量勾选当前筛选结果
- 每份订阅使用独立的高强度随机 Token，可一键轮换并让旧链接立即失效
- 自动识别客户端，并提供 V2Ray/v2rayNG、小火箭、Clash、sing-box、Surge 独立地址与本地二维码
- Clash Meta 智能分流模板：手动选节点、自动延迟优选、故障转移、广告拦截、国内直连和服务分类
- Base64、Clash Meta、sing-box、Surge 模板可在线编辑或复制扩展
- 启用/停用、到期时间、节点选择、流量配额、IP 数和设备数策略
- 24 小时等可配置活跃窗口，管理员可查看或清空 IP/设备绑定
- 订阅访问日志、阻断原因、`Subscription-Userinfo` 流量/到期响应头
- 标准化真实流量上报接口，供自有 Xray/sing-box 节点采集器接入
- 无 npm/pip 运行依赖；Python 标准库 + SQLite，二维码也由本机生成，Caddy 自动 HTTPS
- Docker 隔离、只读应用容器、非 root 用户、自动健康检查
- 数据库和日志自动限龄、限量清理，Docker 日志轮转，设置页与 `dy info` 可查看磁盘占用

## 一键安装（Debian 12）

准备工作：

1. 给域名添加 A/AAAA 记录并指向 VPS。
2. 放行 TCP 80、TCP/UDP 443。
3. 如果使用 Cloudflare，为保证 IP 限制准确，建议该记录保持“仅 DNS”。Cloudflare 代理会让源站看到边缘节点 IP，除非你另外配置可信代理和源站防火墙。

用 root 通过 SSH 执行：

```bash
curl -fsSL https://raw.githubusercontent.com/a2899882/dingyue/main/scripts/install.sh | bash
```

安装器会提示输入域名、管理员账号和密码，随后自动完成 Docker、Caddy 反向代理和 HTTPS。不会要求使用 nano。

若先克隆仓库：

```bash
git clone https://github.com/a2899882/dingyue.git
cd dingyue
sudo bash scripts/install.sh
```

安装位置是 `/opt/subvault/app`，持久数据是 `/opt/subvault/app/data/subvault.db`，机密配置是 `/opt/subvault/app/.env`。

安装完成后直接输入：

```bash
dy
```

菜单可以查看面板地址、更换域名、更新、查看日志、备份、恢复迁移或卸载容器。更换域名时会同步修改 Caddy 域名、`SUBVAULT_PUBLIC_URL` 和面板生成的全部订阅地址；订阅 Token 与数据不会改变。

### 已安装 v0.1.x 的首次升级

旧版本还没有 `dy` 命令，执行下面一行即可升级并安装菜单：

```bash
cd /opt/subvault/app && git pull --ff-only && sudo bash scripts/update.sh
```

升级后输入 `dy`。第一次升级完成后建议在浏览器按一次 `Ctrl+F5`；后续版本的静态资源已经使用版本号和禁止陈旧缓存策略。

## 日常 SSH 命令

```bash
# 打开管理菜单
sudo dy

# 直接查看地址与状态
sudo dy info

# 直接更换域名
sudo dy domain

# 更新（先自动备份）
sudo dy update

# 实时日志
sudo dy logs

# 容器状态
sudo dy status

# 手工备份
sudo dy backup

# 从 /root 或本机备份目录恢复
sudo dy restore

# 清理过期明细并压缩数据库（会先自动备份）
sudo dy compact

# 重启
cd /opt/subvault/app && docker compose restart
```

旧版 Debian Docker 若没有 `docker compose` 子命令，可把命令替换为 `docker-compose`。

## 容量、性能与磁盘

SubVault 是控制面，不转发代理流量。只管理约 100 个节点、几百份订阅且团队正常拉取订阅时，**1 核 CPU、1 GB 内存、20 GB 磁盘通常足够**，可以先继续使用现有 VPS。建议确保至少有 1 GB Swap，并定期用以下命令观察：

```bash
sudo dy info
free -h
swapon --show
docker stats --no-stream
```

出现下列任一情况时再升级到 2 核 2 GB：容器因 OOM 重启、可用内存长期低于约 150 MB、Swap 持续频繁读写、同一台 VPS 还承担代理转发，或短时间内有大量客户端同时更新订阅。20 GB 磁盘对于纯面板数据有很大余量；真实节点流量不会写入本数据库，只保存累计数值和上报明细。

默认磁盘保护如下，均可在 `.env` 中修改：

| 数据 | 默认策略 |
|---|---|
| 访问日志 | 保留 30 天，且最多 100,000 条 |
| 流量上报明细 | 保留 180 天，且最多 100,000 条 |
| 不再活跃的 IP/设备绑定 | 保留 90 天 |
| 自动备份 | 保留 30 天 |
| Docker 容器日志 | 每个容器 10 MB × 3 个文件 |
| 自动清理检查 | 每 6 小时一次 |

SQLite 删除旧记录后会复用空间，但数据库文件不一定立即缩小。需要归还磁盘空间时执行 `sudo dy compact`；命令会先做备份，再清理并执行数据库压缩。系统设置页会显示数据库（含 WAL）、磁盘剩余空间和当前明细数量。

## 流量限制为什么需要节点上报

把同一条 `vless://`、`trojan://` 等节点链接发给多人时，代理服务器只看到节点凭证，无法知道流量来自面板里的哪份订阅。面板自身只能准确控制：

- 谁能下载订阅；
- Token、到期时间、订阅拉取 IP/客户端数量；
- 节点采集器已上报的流量配额。

要准确限制“代理产生的真实流量”，节点必须为每份订阅使用可区分的用户凭证，并把该用户流量上报给面板；达到配额时还应由节点侧停用凭证。接口和接入边界见 [docs/TRAFFIC_REPORTING.md](docs/TRAFFIC_REPORTING.md)。当前 MVP 会在配额耗尽后停止继续下发订阅，但不能远程抹掉朋友客户端里已经缓存的节点。

格式兼容说明：Base64 会原样分发全部支持协议；Clash Meta 和 sing-box 会转换上述常用协议；内置 Surge 模板当前转换 Shadowsocks 与 Trojan，其余协议会跳过，避免生成无法导入的伪配置。

## 防止转发与限制边界

订阅下载限制无法阻止客户端把已经取得的 `vless://`、`trojan://`、VMess 等单节点凭证再次分享。因为代理客户端必须拿到凭证才能连接，隐藏复制按钮、混淆配置或只提供二维码都不是安全措施。

真正的节点级防分享需要：

1. 每份订阅在每个节点使用独立 UUID/密码；
2. 节点采集器按该凭证统计活跃 IP、连接数和流量；
3. 超限后由 Xray/sing-box 节点侧立即停用该凭证；
4. 面板续期或管理员解除绑定后再恢复凭证。

SubVault 当前会明确区分“订阅入口限制”和“节点已上报流量”，不会把 User-Agent 近似识别宣传成硬件级设备锁。完整闭环需要针对你的节点安装方式另行部署节点代理程序。

## 本地开发与测试

```bash
export SUBVAULT_DATA_DIR=/tmp/subvault-dev
export SUBVAULT_ADMIN_USER=admin
export SUBVAULT_ADMIN_PASSWORD='change-this-password'
export SUBVAULT_PUBLIC_URL=http://127.0.0.1:8080
export SUBVAULT_COOKIE_SECURE=0
python3 app.py
```

打开 `http://127.0.0.1:8080`。

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```

## 备份与迁移

`dy backup` 会短暂停止应用写入，把 `data/` 与 `.env` 打包成权限为 600 的 `subvault-时间.tar.gz`，保存在 `/opt/subvault/backups`；随后自动恢复服务。默认保留 30 天。

迁移到新服务器：先按“一键安装”完成新实例，把备份包上传到新服务器的 `/root`，然后执行：

```bash
sudo dy restore
```

菜单会自动列出 `/root/subvault-*.tar.gz` 和本机备份目录中的压缩包，恢复前先校验路径与文件类型，再自动备份现有实例。迁移时建议对“恢复旧域名与配置”选择默认的 `N`，这样会恢复数据库、节点、订阅、模板和原管理员账号，同时保留新服务器的域名与系统密钥。恢复前的数据和 `.env` 仍会留下一份可回滚副本。

## 安全建议

- 域名只用于面板和订阅，不要与节点入站端口混用。
- 使用随机强密码；不要提交 `.env`、数据库或真实节点到 Git。
- 定期执行备份并把备份复制到 VPS 之外。
- 数据库中包含节点链接和订阅 Token；主机失陷时应视为全部泄露并轮换。
- 管理员登录有速率限制、HttpOnly/SameSite Cookie、CSRF 校验和严格安全响应头。
- 未登录页面使用中性文案，管理界面与管理脚本只在认证后下发。这样能减少公开页面暴露的业务关键词，但不能替代域名、源站和访问控制层面的保护。

详细报告方式见 [SECURITY.md](SECURITY.md)。

## 许可证

MIT
