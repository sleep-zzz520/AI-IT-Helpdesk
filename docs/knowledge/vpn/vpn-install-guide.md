---
scenario: vpn
doc_title: VPN 客户端安装指南
error_codes: []
risk: low
action: software.install
version: "1.0"
status: active
valid_from: "2026-01-01"
valid_to: "2099-12-31"
tags: [vpn, 安装, 下载, 客户端]
source_url: docs/knowledge/vpn/vpn-install-guide.md
---

# VPN 客户端安装指南

## 安装前准备

- 操作系统：Windows 10/11（64 位）、macOS 12+、Ubuntu 20.04+
- 安装包来源：公司软件中心（仅此渠道，禁止从互联网下载 VPN 客户端）
- 安装需要本地管理员权限

## 安装步骤

1. 打开软件中心，搜索 "VPN Client"，点击安装
2. 安装完成后首次启动会提示导入服务器配置（域名：vpn.acme.com.cn）
3. 输入域账号（工号@acme）完成注册绑定
4. 安装完成后重启一次电脑，避免网络驱动加载异常

## 常见问题

- 安装失败"找不到设备"：多半是杀软拦截驱动，临时关闭杀软后重装
- 提示已安装但无法卸载：使用软件中心的"修复"功能
- 多设备安装：同一账号最多绑定 3 台设备，超出需在门户解绑

## 与故障排查的区别

本指南只覆盖"安装"，连接失败后的排查（错误码、证书、路由）见
《VPN 客户端配置指南》与《VPN 错误码速查手册》。
