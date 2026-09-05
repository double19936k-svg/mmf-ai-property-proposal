# 安全与隐私说明

MMF 是本机桌面工具。公开仓库只应包含源码、示例配置和脱敏说明，不应包含任何人的真实密钥或客户资料。

## API Key

- 在页面「AI引擎设置」中填写，或使用 `.env` / 环境变量。
- Windows 上默认写入凭证管理器，不写入普通 JSON 配置，也不应出现在日志里。
- `.env`、`config/providers.local.json`、`config/user_settings.json` 已加入 `.gitignore`。
- 示例文件只允许明显假值：`YOUR_QWEN_API_KEY`、`YOUR_KIMI_API_KEY`、`YOUR_API_KEY_HERE`。

## 不会随仓库提供的内容

- 真实 API Key、Bearer Token、Cookie、登录态
- Windows / DPAPI 凭证库文件
- `runs/`、`logs/`、`cache/`、`temp/`、`output/`
- 真实招标文件、真实客户名称对应的原始资料、真实生成成品
- 开发者本机绝对路径

## 使用云端引擎时

选用千问或 Kimi 时，为了生成正文，相关提示会发到对应云端接口。本机 Grok 通过本机已登录的 CLI 访问其服务。不能理解为断网离线写作。

请不要向任何引擎上传涉密、未经授权或含个人信息的材料。

## 如果 Key 曾经泄露

删除文件不能让已经暴露的 Key 重新安全。需要到对应 Provider 后台轮换或作废该 Key。本仓库不会替你撤销任何账号凭证。
