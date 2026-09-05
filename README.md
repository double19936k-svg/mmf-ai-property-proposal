# MMF Desktop / Local

**物业服务方案 / 投标技术方案辅助工具（本机运行）**

版本：**v0.1.0-alpha**  
状态：**Initial Deployable / Alpha**  
不是商用成品，不能替代人工终审。

MMF 把招标或项目资料、需求梳理、可选 AI 引擎、章节规划与 Word/PPT 输出串成一条本机工作流。浏览器只是操作界面；服务默认只监听 `127.0.0.1`，不主动开放到局域网或公网。

当前定位：**AI 辅助生成初稿 + 人工最终审核。**  
不是全自动投标，也不能替代专业判断。

---

## 当前状态

| 项 | 说明 |
|---|---|
| 版本 | `0.1.0-alpha` |
| 阶段 | 初始可部署（Initial Deployable） |
| 基线 | 长文按章生成、检查点续写、Word/PPT 渲染、本机安装启动 |
| 不是 | commercial ready / fully automated bidding / 无需人工审核 |

已验证过的能力以本仓库源码和文档为准。一次样例的页数或观感，不能推广成每个项目都会自动达到同样质量。

---

## 核心能力

- **招标 / 需求导入**：PDF、Word、文本等常见格式先在本机解析，再进入需求确认。
- **方案结构**：完整物业服务方案按既定章节计划生成，而不是让模型自己决定写几章。
- **长文编排**：按章调用 AI，章节完成后留检查点，中断后可从当前章继续。
- **Word / PPT 输出**：Word 为默认交付；PPT 需本机安装 Node。
- **多引擎**：千问（云端 API Key）、可选本机 Grok Bridge、Kimi（可配 Key 试用）、Mock（只验证流程）。
- **治理门禁**：对过短长文、明显人员编制承诺、内部编号泄漏等做拦截或提示。不能替代审稿。
- **本机凭证**：API Key 写入 Windows 凭证管理器或环境变量，不进 Git，不进普通配置文件。

---

## 结构示意

```text
招标/Brief ──► 本机解析与需求确认 ──► 章节计划 / 篇幅预算
                                          │
                                          ▼
                               Provider（千问 / Kimi / Grok / Mock）
                                          │
                                          ▼
                               按章生成 + 检查点/续写 + QA 门禁
                                          │
                                          ▼
                               Word / PPT ──► 人工终审
```

界面运行在本机浏览器：`http://127.0.0.1:3050/`。本仓库是源码候选包，不包含真实运行记录、真实招标或个人密钥。

---

## 快速启动

环境要求：

- Windows
- Python 3.10 或更高
- 如需 PPT：再安装 Node.js。只出 Word 可以不装 Node

步骤：

1. 复制本仓库到本机任意可写目录（不要把密钥写进仓库）。
2. 双击 `首次安装.cmd`，等待提示安装完成。安装会在 `runtime/python` 创建隔离虚拟环境。
3. 之后可双击 `启动MMF.cmd`。
4. 若浏览器未自动打开，访问 `http://127.0.0.1:3050/`。
5. 首次打开时完成「首次配置」：选择输出目录、默认引擎、默认 Word 或 PPT。
6. 到「AI引擎设置」填写你自己的 API Key，保存后点「测试连接」。

命令行等价方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\launch_mmf.ps1
```

不要把 `runs/`、`logs/`、`output/`、`.env`、`config/providers.local.json` 提交到 Git。

---

## Provider 配置方法

MMF **不会**在源码或示例里提供真实 Key。请使用你自己的账号。

1. 复制 `.env.example` 为 `.env`（可选），或直接在页面「AI引擎设置」中填写。
2. 示例值只能是占位符，例如 `YOUR_QWEN_API_KEY`。
3. 保存后必须「测试连接」。未测试通过的引擎不能用于正式生成。
4. Windows 上 Key 默认进入凭证管理器；也可用环境变量：

| 引擎 | 环境变量 | 说明 |
|---|---|---|
| 千问 / 万相 | `DASHSCOPE_API_KEY` | 国内云端。可与图片服务共用一把 Key |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | 可试用；长文表现仍在观察 |
| Grok Imagine | `XAI_API_KEY` | 可选图片服务 |
| Local Grok Bridge | 无 API Key 字段 | 依赖本机已安装并登录的 Grok CLI |

配置示例见：

- `.env.example`
- `config/providers.example.json`
- `config/user_settings.example.json`

`config/providers.local.json` 与 `config/user_settings.json` 由本机首次运行生成，已列入 `.gitignore`。

---

## 当前限制

- 必须人工终审，不能直接当盖章投标稿。
- 三百页级正式技术标尚未充分验证。
- 不同引擎的篇幅、写法、速度、费用不一致。
- 扫描件、复杂表格、多附件仍可能识别不全。
- WPS 分页兼容性未作为已通过项。
- 人员编制、SLA、收费、责任边界必须人确认。
- 选用千问或 Kimi 时，生成提示会发到对应云端，不是完全离线写作。
- PPT 观感需要人看，不能只看“能导出”。
- 本版本不是 commercial ready，也不是 fully automated bidding。

---

## 安全与隐私

- **不要**把真实 API Key、Cookie、Token 写入仓库、示例、日志或 Issue。
- 本候选包已排除：凭证库、`.env`、真实 runs/logs/output、个人绝对路径、真实客户招标与成品。
- 知识条目已做来源路径与客户名称脱敏；仍可能包含行业方法论，请按你所在机构的合规要求使用。
- 招标文件先在本机解析。一旦选用云端引擎，相关提示会离开本机。
- 请不要上传涉密、未经授权或含个人信息的材料。
- 若 Key 曾经出现在 Git、聊天记录或共享日志中，删除文件不能让 Key 重新安全，需要到对应 Provider 后台轮换。

详见 `docs/SECURITY.md` 与 `LICENSE_PENDING.md`。

---

## 仓库内容

```text
app/            应用源码、规划、长文编排、治理、渲染、Provider
broker/         本机会话
config/         公开示例配置（不含本机密钥）
docs/           产品介绍、安全说明、开发历程摘要
examples/       虚构 Demo，不是真实项目
providers/      Local Grok Bridge
static/         本机 Web UI
tests/          单元/流程测试
tools/          安装与启动脚本
```

默认不上传：`runs/`、`logs/`、`cache/`、`temp/`、`output/`、`credentials/`、本机 Python 运行时、真实招标、内部 Sprint 产物。

---

## 许可

当前公开仅用于展示与评估。正式开源许可尚未确定。见 `LICENSE_PENDING.md`。
