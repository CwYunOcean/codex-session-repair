# 贡献指南

Python 3.11+，测试和演示均使用标准库。

```shell
python -m unittest discover -v
python scripts/reproduce.py
python scripts/build.py
python dist/codex-session-repair.pyz --help
```

有 Codex CLI 0.169.0 时可额外运行 `python scripts/reproduce.py --codex`，测试仅使用临时数据目录和本地模拟 Responses 服务。较旧 CLI 不支持演示使用的全部选项时，先用默认演示。

修复逻辑的修改至少保留：可见消息与工具 I/O 的原始字节、每条记录的字节边界、元数据数组对齐、原生锁互斥、备份校验、恢复对新历史的保护。新增类型必须引用对应 Codex 源码版本，并加入合成用例。遇到未知类型应给出明确错误，不能递归删除任意用户对象。

不要提交真实 rollout、SQLite 数据库、认证文件或私人交接文件。测试历史在 `tests/fixtures.py` 内构造；issue 可提供 `inspect --json` 的脱敏结果，检查路径、工作目录与项目名后再贴出。
