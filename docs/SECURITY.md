# 安全与隐私说明

MMF 是本机桌面工具。公开仓库只应包含源码、示例配置和脱敏说明，不应包含任何人的真实密钥或客户资料。

## API Key

- 在页面「AI引擎设置」中填写，或使用 `.env` / 环境变量。
- Windows 上默认写入凭证管理器，不写入普通 JSON 配置，也不应出现在日志里。
- 凭证管理器只说明 Key 默认放在本机账户下。能登录该 Windows 账户，或能读取本机凭证的程序，仍可能拿到 Key。不要把它理解成绝对不会暴露。
- `.env`、`config/providers.local.json`、`config/user_settings.json` 已加入 `.gitignore`。
- 示例文件只允许明显假值：`YOUR_QWEN_API_KEY`、`YOUR_KIMI_API_KEY`、`YOUR_XAI_API_KEY`、`YOUR_ZHIPU_API_KEY`。

## 不会随仓库提供的内容

- 真实 API Key、Bearer Token、Cookie、登录态
- Windows / DPAPI 凭证库文件
- `runs/`、`logs/`、`cache/`、`temp/`、`output/`
- 真实招标文件、真实客户名称对应的原始资料、真实生成成品
- 开发者本机绝对路径

## 数据去向

程序和页面在本机。招标文件的解析也在本机完成。本机运行不是完全离线。

- 选择千问、Kimi 或智谱 GLM 时，提示词、必要的招标内容和生成请求会发到对应开放平台。Kimi 使用 `MOONSHOT_API_KEY`，国内默认接口为 `https://api.moonshot.cn/v1`，示例模型为 `kimi-k3`，只作为文字模型，不生成图片。
- 登录 Qoder、ZCode 或 Cursor 时，生成请求发往对应账户服务，不是留在本机的离线模型。
- Grok Bridge 是本机桥接程序。它通过本机已登录的 Grok 程序访问 Grok 的在线服务，不要和云端 API、完全离线模型混为一谈。
- 服务商如何保存、留存和使用收到的内容，以该服务商的规则为准。招标文件、商业秘密和客户资料是否适合提交，由使用者自行判断。

请不要向任何引擎上传涉密、未经授权或含个人信息的材料。

## 如果 Key 曾经泄露

删除文件不能让已经暴露的 Key 重新安全。需要到对应 Provider 后台轮换或作废该 Key。本仓库不会替你撤销任何账号凭证。
