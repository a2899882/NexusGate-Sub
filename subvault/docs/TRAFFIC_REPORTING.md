# 真实流量上报与节点侧执行

## 接口

`POST /api/v1/usage` 接收某一订阅的增量流量。认证密钥为服务器 `.env` 中的 `SUBVAULT_USAGE_REPORT_KEY`。

```bash
curl -X POST 'https://sub.example.com/api/v1/usage' \
  -H 'Authorization: Bearer YOUR_USAGE_REPORT_KEY' \
  -H 'Content-Type: application/json' \
  -d '{
    "subscription_token": "SUBSCRIPTION_TOKEN",
    "upload_bytes": 1048576,
    "download_bytes": 2097152,
    "source": "node-us-01"
  }'
```

字段均为本周期增量，不是累计值。成功响应中的 `allowed` 表示订阅目前是否仍满足启用、到期和配额策略。

## 正确的完整闭环

1. 每份订阅在每个节点拥有独立用户标识（VLESS UUID、Trojan 密码等）。
2. 采集器从 Xray stats API、sing-box Clash API 或节点数据库取得各用户增量。
3. 采集器维护“节点用户 → 订阅 Token”的映射并定期调用上报接口。
4. 当响应 `allowed=false`，采集器从节点配置/API 停用该用户。
5. 管理员重置配额或续期后，采集器再恢复用户。

当前仓库提供面板端接口，没有自动修改第三方节点。不同 Xray/sing-box 安装方式的用户管理接口差异很大，贸然自动写配置可能造成整台节点离线。

## 安全要求

- 上报密钥只放在可信节点或独立采集器，不要放进客户端订阅。
- 建议采集器每 1–5 分钟批量上报一次，并把未成功上报的增量持久化，避免重启后丢失或重复。
- 面板 API 必须使用 HTTPS。
- 泄露上报密钥后，在 `.env` 中更换密钥并重启服务。
