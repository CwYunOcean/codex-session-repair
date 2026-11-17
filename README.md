# Codex Session Repair

离线诊断并恢复 Codex 切换 Responses 后端后出现的 `invalid_encrypted_content`。适用于通过 CC Switch 或直接编辑 `config.toml` 换源后，**新会话能用、旧会话立即报 HTTP 400** 的情况。

Python 3.11+，运行时零第三方依赖。支持 Windows、macOS、Linux。默认只预览；实际修复会先备份，并保持 JSONL 每条记录的字节长度，保护分页历史索引。

[下载最新版](https://github.com/CwYunOcean/codex-session-repair/releases/latest) · [报告问题](https://github.com/CwYunOcean/codex-session-repair/issues)

## 快速开始

下载 Release 中的源码 ZIP 并解压，或克隆仓库。进入目录后无需安装 Python 包：

```powershell
git clone https://github.com/CwYunOcean/codex-session-repair.git
cd codex-session-repair
python -m codex_session_repair list
python -m codex_session_repair inspect <完整会话UUID>
python -m codex_session_repair repair <完整会话UUID>
```

预览符合预期后，**先停止并卸载目标会话**，再执行：

```powershell
python -m codex_session_repair repair <完整会话UUID> --apply --offline
codex resume <完整会话UUID>
```

`--offline` 表示你已确认目标会话不在运行；工具还会尝试取得 Codex 的原生写入锁。无法取得锁时直接退出，不会关闭其他窗口。对于没有原生写入锁的旧客户端，仍然必须彻底退出目标客户端后操作。

桌面客户端可能在会话界面关闭、显示“未加载”或没有正在采样时仍持有后台写入锁。遇到此情况，保存工作，完整退出持锁的 Codex 客户端，然后在普通 PowerShell/终端运行修复工具，再重新打开会话。不要删除锁文件或强行覆盖；删除文件并不能释放进程已经持有的锁。

Windows 也可以双击 `codex-session-repair.cmd`，按序号选择会话、查看预览，再输入完整 UUID 确认写入。命令行可直接向此入口传参。双击入口从全局最近的主会话中选择，不会误把工具目录当作会话目录。

如果 Python 版本过旧，先安装 Python 3.11 或更新版本；`python --version` 可检查当前版本。

## 常用命令

```powershell
# 只看指定项目，含 Windows 大小写与 \\?\ 路径兼容
python -m codex_session_repair list --cwd F:/JLC

# 查看归档会话
python -m codex_session_repair list --include-archived

# 指定不同 Codex 数据目录；全局参数放在子命令之前
python -m codex_session_repair --codex-home D:/codex-data inspect <UUID> --json

# 也可明确指定已有 JSONL 文件；实际写入仅允许所选数据目录内的会话
python -m codex_session_repair inspect "C:/Users/me/.codex/sessions/.../rollout-....jsonl"

# 备用恢复：导出明文交接，让全新会话读取它继续
python -m codex_session_repair export <UUID>
python -m codex_session_repair export <UUID> --output D:/private/handoff.md

# 使用修复命令输出的备份目录
python -m codex_session_repair restore "<备份目录>"
python -m codex_session_repair restore "<备份目录>" --apply --offline
```

`export` 保留可见用户/助手文字和明文压缩摘要。工具原始输出、系统提示、密文及外部历史前缀不在这个交接文件中；完整原始数据保留在备份里。导出不会覆盖已有文件。

备份默认位于：

```text
CODEX_HOME/backups/codex-session-repair/<时间>-<UUID>/
  original.jsonl    完整原文件
  manifest.json    哈希、原路径、长度、版本
  handoff.md       新会话可读取的明文交接
```

实际恢复必须匹配备份中的修复后 SHA-256。修复后已经有新消息时，工具会拒绝覆盖它们；此时使用备份中的交接资料。重复修复已清理的文件会返回 `unchanged`。

如需安装一个固定命令：

```powershell
python -m pip install .
codex-session-repair list
```

## 原因与修复范围

Responses 历史中的 `reasoning.encrypted_content` 等字段是服务端生成的不透明内容。Codex 将它们写入会话，并在续聊时重新发送。另一后端、账号或不兼容的解密环境可能拒绝这些内容。

CC Switch 经常复用 `model_provider = "custom"`，只更新 `base_url` 和认证信息。相同的 `custom` 标识不意味着密文可以跨后端使用。详细依据、排查步骤和本次脱敏案例见 [问题分析](docs/root-cause.zh-CN.md)。

| 历史内容 | 处理方式 |
| --- | --- |
| 加密推理记录 | 退出模型回放；可见摘要保留为本地 `agent_reasoning` 事件 |
| 加密代理消息片段 | 保留可见文字，移除密文并加入缺失标记 |
| 消息或工具调用的可选密文字段 | 移除这些字段，保留可见消息、调用参数和输出 |
| 压缩历史中的加密推理 | 移除对应条目，并同步其平行元数据数组 |
| 加密压缩条目 | 仅在同一记录已有明文摘要时替换；否则拒绝修复，建议交接 |
| 普通聊天、工具调用/输出、明文压缩摘要 | 保留 |
| 未知加密类型、外部 `history_base` | 拒绝单文件修复，给出交接路径 |

工具无法解密或重新生成隐藏推理，也不能恢复只存在于密文中的代理结论。首次续聊应根据项目文件、明文摘要和已保存的结果核对状态。

## 为什么保持字节长度

新版本的分页历史在 `thread_history_*.sqlite` 中记录 JSONL 的字节位置和记录序号。直接删除行、整文件重新格式化、只改总长度，都可能破坏这些引用。

本工具逐条修改受影响的记录，再在 JSON 末尾补空格，保持**每条记录**的原字节长度、换行形式和位置。推理记录变成合法的非回放事件，避免删除物理行。压缩元数据数组也会保持对应关系。无法放入原字节预算时，在写入前拒绝修复。

写入顺序是：校验文件 → 取得写入锁 → 回读确认未变化 → 写入并校验备份 → 同目录临时文件和 `fsync` → 覆盖前再次校验 → 原子替换 → 回读哈希。配置文件和 SQLite 数据库不需要改写。

这保护已知的索引格式；不是对未来 Codex 存储协议的无限兼容承诺。已在 Windows 的 Codex CLI **0.169.0** 验证原生恢复，并对 **0.162.1** 创建的真实分页会话完成预览和偏移检查。

## 判断是否适合使用

| 现象 | 建议 |
| --- | --- |
| 换源后只有旧会话报 `invalid_encrypted_content` | 检查旧历史，先预览修复 |
| 新会话也报错 | 先检查模型、接口协议、认证或网关兼容性 |
| 只有密文字段，没有报错 | 正常会话也含密文；不要把它直接认定为损坏 |
| 修复后 inspect 仍显示一条历史错误 | 错误日志保留用于追溯；检查当前密文字段及下一次续聊结果 |
| `array_above_max_length`、工具格式错误等另一错误 | 属于其他协议问题，本工具不会直接删除工具调用 |

切换提供商时优先开始新会话，并携带明文交接。为不同提供商设置不同 ID 有助于辨认，但不能让既有密文获得跨后端兼容性。若同一个网关在不兼容的后端间动态路由，这类错误也可能在未改配置时重现。

## 验证与可复现演示

```powershell
python -m unittest discover -v
python scripts/reproduce.py

# 可选：用已安装的 Codex 对本地模拟接口完成真实 resume 流程
python scripts/reproduce.py --codex
# 或指定 exe 路径
python scripts/reproduce.py --codex "C:/path/to/codex.exe"
```

默认演示在临时目录建立两个本地模拟后端：A 接受自身签发的测试内容；B 拒绝 A 的内容，返回 400；修复后 B 接受历史，返回 200；最后恢复原始字节。

`--codex` 会通过本机 Codex 创建一个临时测试会话，在同一个 `custom` ID 下切换本地接口，实际执行失败与修复后的 `resume`。模拟响应包含推理项和 SSE 消息，流程不调用真实商业模型。Codex 自身的平台或版本检查行为由客户端控制；测试配置关闭了启动更新检查和 analytics。临时配置使用假凭据，测试不会操作用户原会话。

演示使用 HMAC 模拟签发方边界，**不声称复现 OpenAI 私有的加密实现**。真实网关能否接受请求，以恢复后的一次正常续聊为准。

测试覆盖逐行偏移、分页引用、UTF-8/CRLF/BOM、代理消息、压缩数组、原生跨进程锁、备份失败、文件竞争、幂等、恢复保护和诊断脱敏。GitHub Actions 提供 Windows/macOS/Linux 与 Python 3.11/3.12/3.13 的测试矩阵。

## 发布产物

```powershell
python scripts/build.py
python dist/codex-session-repair.pyz --help
```

`dist/` 包含单文件 Python zipapp、源码 ZIP 和 `SHA256SUMS.txt`，可作为 GitHub Release 附件。相同源码和相同 Python/zlib 环境下生成的 ZIP 时间戳固定，可重复构建。

只下载 `codex-session-repair.pyz` 时，可从任意目录运行 `python codex-session-repair.pyz list`；不需要解压或安装包。

原会话、备份、交接资料和认证文件可能含私人信息。诊断只输出白名单元数据；备份和交接应保存在仓库外，上传 issue 前仍需检查路径和项目名。参见 [安全说明](SECURITY.md)。

MIT License。独立社区工具，与 OpenAI、CC Switch 及网关服务商没有隶属关系。
