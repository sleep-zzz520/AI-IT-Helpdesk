---
scenario: vpn
doc_title: VPN 证书续期 SOP
error_codes: ["800", "720"]
risk: low
action: vpn.renew_certificate
version: "1.0"
status: active
valid_from: "2026-01-01"
valid_to: "2099-12-31"
tags: [vpn, 证书, 续期, 过期]
source_url: docs/knowledge/vpn/cert-renewal.md
---

# VPN 证书续期 SOP

## 症状与判断

用户报障"VPN 连不上"时，先区分故障类型。**证书过期**的典型特征：

- 拨号时报错 **Error 800**（无法建立 VPN 连接），且监控平台显示账号证书状态为 **已过期**
- 客户端提示"证书已过期"或"安全网关证书无效"
- 近期证书即将到期（有效期不足 30 天）的用户开始集中报障

需要与以下情况区分：
- Error 800 但证书状态正常 → 客户端配置问题（见《VPN 客户端配置指南》）
- Error 720（无法建立与远程计算机的连接）→ 拨号连接损坏，需重建连接
- Error 809（无法建立计算机与 VPN 服务器之间的网络连接）→ 网络/防火墙问题，与证书无关

## 处理步骤

1. 通过监控平台确认账号证书状态（cert 状态 = expired / valid / not_found）
2. 证书已过期 → 执行证书续期（certutil -renew）
3. 续期成功后重新拨号验证连接
4. 验证通过后告知用户，关闭工单

## 风险说明

证书续期是**低风险操作**：只更新用户自己的证书，不影响他人；失败可回滚（重新生成）。可自动执行。

## 操作命令

```bash
certutil -renew   # 更新当前用户证书
# 续期后需重新拨号建立 VPN 连接
```
