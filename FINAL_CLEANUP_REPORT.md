# MMF v0.1.1-alpha 发布卫生整改报告

记录日期：2026-11-17

本次只做发布卫生。没有新增功能，没有改 Provider 实现，没有改生成流程，没有改 `LICENSE_PENDING.md`，也没有重新打包。远程同步是在本地清理之后单独完成的，核验结果见第 11 节。

## 1. 修改摘要

- Kimi 文档已与现有代码对齐：可以配置 API Key，并作为文字模型使用。
- 当前 main 和全部本地历史中的开发者本机绝对路径已清除。
- 本地历史已重写。重写前保存了完整 git bundle。远程 main 和 tag 已同步，并以 `git ls-remote` 复核。
- 构建清单保留 2026-09-13 的真实构建时间，并写明 2026-11-17 没有重新构建。
- 用户说明区分了本机页面、云端模型、账户引擎和 Grok Bridge。

## 2. 修改文件

- `README.md`
- `README_用户使用说明.md`
- `docs/README_用户使用说明.md`
- `docs/SECURITY.md`
- `docs/MMF_产品介绍_一页版_V0.1.md`
- `docs/MMF_产品介绍与试用说明_V0.1.md`
- `docs/RUN_J_DELIVERY.md`
- `docs/RUN_J_ZCODE_INDEPENDENT_GATE.md`
- `CHANGELOG.md`
- `build_manifest.json`
- `release_manifest.json`
- `tests/test_cleanroom_deployment.py`
- `tests/verify_round3_resume.py`
- `tools/assemble_release.py`
- 历史中的三个单行临时路径文件已从全部提交删除。它们不是压缩规划模块；`app/compression/` 仍在。

## 3. Kimi Provider

代码未改。当前实现是 OpenAI 兼容接口：

- Provider 名称：`kimi_moonshot`
- 环境变量：`MOONSHOT_API_KEY`
- 国内默认接口：`https://api.moonshot.cn/v1`
- 示例模型：`kimi-k3`
- Key 在 platform.kimi.com 创建，页面用「连接引擎」保存
- 文字生成可用。图片生成不可用，页面只保留图片理解说明

已删除「尚未完成官方认证，因此不能使用」的说法。长文仍需人工复核，文档没有把它写成已完成的正式验收引擎。

## 4. Git 历史扫描

重写前，完整历史共 13 个提交。开发者本机路径出现在构建清单、发布清单、Run J 说明、清洁部署测试和发布组装脚本中。另有三个只含临时路径的单行文件。

重写后本地共 14 个提交，再次扫描：

- 用户名和开发者项目目录：0 处
- `C:\Users` 与 `Users/`：0 处
- 上述单行临时文件：0 处

2026-11-17 再次核验时，远程只有 `HEAD`、`refs/heads/main` 和 `refs/tags/v0.1.1-alpha`。对这组引用能到达的 17 个提交重新扫描后，用户名、开发者项目目录和 `C:\Users` 仍是 0 处。

仍保留的不是个人路径：

- `app/paths.py` 用目录名 `mmf006e_cleanroom` 识别外来配置
- 旧测试把同名目录放在系统临时目录下
- `tools/run_j_gate_checks.ps1` 使用当前用户的 `USERPROFILE` 查找缓存，没有写死某个人的目录
- `AppData` 只出现在 `Path.home()` 或环境变量拼接中

## 5. 历史重写与备份

已重写本地历史，分两次完成：先替换绝对路径，再去掉残留用户名、项目目录名和三个临时文件。

本地 tag `v0.1.1-alpha` 仍在，指向重写后的同一条合并提交，提交说明不变。标签对象是 `495e5f6b05dd16b24ee3103f65068ebee9dcefd8`，对应提交是 `f8a88f00a2d46e9bc7105ac2dd3dcc15712ea06b`。

备份是重写前的完整 bundle，文件名为 `mmf-ai-property-proposal-pre-cleanup-20261117.bundle`，约 4.1 MB，放在本机「正式发布版」目录。报告不写该目录的绝对路径。

## 6. 构建追溯

`build_manifest.json` 仍记录：

- 版本：`0.1.1-alpha`
- 构建时间：`2026-09-13T17:33:17+08:00`
- 构建 ID：`MMF006E-R2.7-5ebcec9064d2-20260913173317`
- 源码哈希保持原值

新增 `public_record_date: 2026-11-17`，并明确 `packaged_dist_rebuilt_on_public_record_date: false`。原先的本机打包路径已删除，`last_packed_dist` 为 `null`。

没有重新构建发布包，因此没有新的 SHA256。2026-11-17 只是文档和脱敏记录日期，不是新的二进制构建日期。已经放在「正式发布版」里的客户端和压缩包也没有按这次清理后的源码重打。

`release_manifest.json` 不是当前发布清单。运行程序不读取它。`tools/assemble_release.py` 会在组装旧候选包时重写它，`tools/pack_portable.py` 只把它复制到打包目录。文件里的 `0.1.0-alpha`、`2026-09-04` 构建时间、`kimi_moonshot_preset_only` 和 “Kimi is not marked available” 都保留为当时的记录，没有改成 2026-11-17。文件开头已标明 `current_release_record: false`。

`config/app.json` 的默认端口仍是 3050，服务代码未改。`启动MMF.cmd` 实际传入 3042，用户说明已按这个启动方式改写。

## 7. 数据安全说明

用户说明、README 和 `SECURITY.md` 现在分开写了四件事：

- 程序和页面在本机，只监听 `127.0.0.1`，这不是完全离线
- 招标文件解析在本机
- 千问、Kimi、智谱 GLM，以及 Qoder、ZCode、Cursor，会把提示词和必要的招标内容发到对应服务商
- Grok Bridge 是本机桥接程序，仍访问 Grok 的在线服务

API Key 默认进入 Windows 凭证管理器。文档不再说密钥绝对不会暴露，并要求保管本机账户。

## 8. 测试

已做：

- 修改过的 JSON 可解析
- 修改过的 Python 文件可通过编译
- Kimi 文档与 `config/providers.example.json`、页面提示一致
- 当前文件和全部本地历史的路径扫描
- `tests/test_mmf006e_r2_stability_freeze.py` 与 `tests/test_mmf007_compression.py`：17 项通过，用时 112.90 秒

未做：

- 全量测试
- 应用启动和页面冒烟
- 重新打包及 SHA256

未做的项目不能视为通过。这次没有改运行逻辑，所以没有重启正在使用的服务。

## 9. 尚未解决

- GitHub 仍可通过已知 SHA 读到重写前的合并提交 `6a87db865979ff142299121df6020dd7eca47f3d`。它不在任何远程分支或标签上。该提交里的清洁部署测试仍有一处开发者项目目录名。没有第二次重写，也没有删除这个对象。
- `config/app.json` 默认端口 3050 与启动脚本 3042 并存。这是原有行为，本次未改代码。
- 桌面客户端和压缩包不是这次清理后的源码重新构建的。

## 10. 能否冻结 v0.1.1-alpha

远程 main 与 Release 标签上的用户手册、安全说明已经对齐。可以停止本阶段的功能性开发。

不能把“GitHub 上任何已知旧 SHA 都已消失”说成已经完成。`6a87db8` 仍然能按 SHA 访问，直到 GitHub 自己回收不可达对象。

## 11. 远程核验

核验命令是 `git ls-remote`，对照 `git rev-parse HEAD`、`git rev-parse v0.1.1-alpha` 和 `git show-ref --tags`。远程只有这几项：

- `HEAD` 与 `refs/heads/main`：`210c8c417a4e7eeca5bc98459d729ae4699d276d`
- tag 对象 `refs/tags/v0.1.1-alpha`：`495e5f6b05dd16b24ee3103f65068ebee9dcefd8`
- tag 指向的提交：`f8a88f00a2d46e9bc7105ac2dd3dcc15712ea06b`

本地与这三项一致。没有其他远程分支或标签。

Release 页面没有新建，也没有删除。名称仍是 `MMF v0.1.1-alpha (R2): Stability, Long-Form Orchestration & Fail-Soft Delivery`，发布记录目标是 `main`，附件数量为 0。页面上的提交号是 `f8a88f0`，不是 `6a87db8`。页面写着 main 比该 tag 超前 6 个提交，这 6 个提交是标签之后的产品更新和本次卫生记录，不是 tag 没有同步。

以上是文档同步之前的核验。当时标签里的用户手册仍是旧文，随后的处理见第 12 节。

备份文件 `mmf-ai-property-proposal-pre-cleanup-20261117.bundle` 仍在本机「正式发布版」目录，大小 4112106 字节。

## 12. 用户文档与 Release 标签对齐

标签 `v0.1.1-alpha` 原先指向清理后的合并提交，但该快照里的用户手册仍写着 Kimi 不能使用。main 上的说明已经改过。本次在该合并提交上只更新用户文档，没有改代码，也没有重写旧提交。

修改文件：

- `README.md`
- `README_用户使用说明.md`
- `docs/README_用户使用说明.md`
- `docs/SECURITY.md`
- `docs/MMF_产品介绍_一页版_V0.1.md`
- `docs/MMF_产品介绍与试用说明_V0.1.md`

修改内容：Kimi 改为可配置 API Key 的文字模型，接口 `https://api.moonshot.cn/v1`，示例模型 `kimi-k3`，不能生成图片。用户说明和 `SECURITY.md` 区分本机运行、云端模型调用和 Grok Bridge，并说明 Windows 凭证管理器不是绝对安全。

标签记录：

- 原 tag 对象：`495e5f6b05dd16b24ee3103f65068ebee9dcefd8`
- 原 tag 提交：`f8a88f00a2d46e9bc7105ac2dd3dcc15712ea06b`
- 新 tag 对象：`4bc406f8527d180d56e6400175a22cfe892dbde9`
- 新 tag 提交：`1ca49ad89987d430a2ce3c359556fbe702698b39`

原 tag 对象保存在本地引用 `refs/backup/v0.1.1-alpha-pre-doc-sync`，没有推送到远程。

Release 页面仍是原来的 `v0.1.1-alpha`，没有新建，也没有删除。同步前附件数量为 0，没有二进制资产被替换或删除。这次没有重新打包。

验证：上述 6 个文件与 main 的对应文件没有差异。标签快照中不再有「尚未完成官方认证」或「不能当作可用引擎」。提交只包含这 6 个 Markdown 文件。
