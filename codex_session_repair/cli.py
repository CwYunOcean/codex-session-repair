"""Small CLI and a Windows-friendly interactive entry point."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .core import (
    RepairError, apply_plan, codex_home, export_handoff, find_session,
    inspect_snapshot, list_sessions, load_snapshot, make_plan, restore_backup,
)


def _display(value: object) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))


def _show(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if "sessions" in report:
        for index, row in enumerate(report["sessions"], 1):
            print(f"{index:2}. {row['session_id']}  {_display(row.get('timestamp', ''))}")
            print(f"    {_display(row.get('cwd', ''))}  Codex {_display(row.get('cli_version', ''))}")
        if not report["sessions"]:
            print("没有找到符合条件的主会话；可用完整 JSONL 路径执行 inspect。")
        return
    if report.get("mode") == "preview":
        print(f"目标会话：{report['session_id']}")
        print(f"预览：将修改 {report['changed_lines']} 行；{report['original_bytes']} 字节及所有行边界保持一致。")
        print(f"剩余密文字段：{report['remaining_encrypted_fields']}")
        print("实际写入：先停止并卸载目标会话，再使用相同命令追加 --apply --offline。")
        return
    if report.get("mode") == "applied":
        print(f"已修复会话：{report['session_id']}（{report['changed_lines']} 行）")
        print(f"备份：{_display(report['backup'])}")
        print(f"明文交接：{_display(report['handoff'])}")
        print(f"现在可重新打开原会话，或运行：codex resume {report['session_id']}")
        print("隐藏推理和仅加密的代理片段无法恢复；首次续聊需重新核对项目状态。")
        return
    if report.get("mode") in {"unchanged", "already_original", "restored", "restore_preview"}:
        descriptions = {
            "unchanged": "无需修复，未写入文件。", "already_original": "原文件已在原始状态，无需恢复。",
            "restored": "已从校验过的备份恢复原文件。",
            "restore_preview": "备份校验通过，可恢复；实际恢复追加 --apply --offline。",
        }
        print(descriptions[report["mode"]])
        return
    if "output" in report:
        print(f"已导出：{_display(report['output'])}")
        print("在新会话中要求 Codex 读取此文件，再给出继续工作的指令。交接文件可能含私人项目资料。")
        return
    print(f"会话：{report['session_id']}")
    print(f"目录：{_display(report.get('cwd', ''))}")
    print(f"文件：{_display(report['path'])}")
    print(f"Codex：{_display(report.get('cli_version', ''))}；历史模式：{_display(report.get('history_mode', 'unknown'))}")
    print(f"加密字段：{report['encrypted_fields']}；历史报错记录：{len(report['observed_error_lines'])}")
    config = report["current_config"]
    print(f"当前配置：{_display(config.get('model_provider', 'unknown'))} → {_display(config.get('base_url', 'unknown'))}")
    print("历史端点未写入 rollout；仅凭密文存在，不能判定会话已损坏。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="codex-session-repair",
        description="离线诊断和修复 Codex 换后端后的 invalid_encrypted_content。默认只预览。",
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--codex-home", metavar="DIR", help="覆盖 CODEX_HOME；放在子命令之前")
    commands = result.add_subparsers(dest="command")
    listing = commands.add_parser("list", help="列出最近的主会话")
    listing.add_argument("--cwd", help="只列出指定工作目录的会话")
    listing.add_argument("--include-archived", action="store_true")
    listing.add_argument("--limit", type=int, default=20)
    inspection = commands.add_parser("inspect", help="诊断完整 UUID 或 JSONL 路径，不输出密文/聊天内容")
    inspection.add_argument("session")
    repair = commands.add_parser("repair", help="预览修复；--apply --offline 才会写入")
    repair.add_argument("session")
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--offline", action="store_true", help="确认目标会话已停止并卸载；还会检查原生写入锁")
    export = commands.add_parser("export", help="导出明文交接文件，供新会话读取")
    export.add_argument("session")
    export.add_argument("--output", type=Path, help="默认为 CODEX_HOME/backups/codex-session-repair/exports 下的新文件")
    restore = commands.add_parser("restore", help="校验并预览恢复备份；拒绝覆盖修复后新增的历史")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--offline", action="store_true")
    for command in (listing, inspection, repair, export, restore):
        command.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return result


def _interactive(home: Path) -> int:
    print(f"Codex Session Repair {__version__}\nCODEX_HOME: {_display(home)}")
    rows = list_sessions(home)
    _show({"sessions": rows})
    if not rows:
        return 0
    try:
        selected = input("输入会话序号查看，或回车退出：").strip()
        if not selected:
            return 0
        number = int(selected)
        if number < 1 or number > len(rows):
            raise ValueError
    except ValueError as exc:
        raise RepairError("会话序号无效。") from exc
    snapshot = load_snapshot(find_session(home, rows[number - 1]["session_id"]))
    _show(inspect_snapshot(snapshot, home))
    choice = input("输入 1 预览修复，2 导出交接，或回车退出：").strip()
    if choice == "2":
        output = _default_export(home, snapshot.session_id)
        _show(export_handoff(snapshot, output))
    elif choice == "1":
        plan = make_plan(snapshot)
        _show(plan.report())
        if plan.changes:
            confirmation = input("先停止并卸载目标会话。确认写入时请输入完整会话 UUID，或回车取消：").strip()
            if confirmation == snapshot.session_id:
                _show(apply_plan(plan, home, offline=True))
            elif confirmation:
                raise RepairError("UUID 不匹配，已取消。")
    return 0


def _default_export(home: Path, identifier: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return home / "backups" / "codex-session-repair" / "exports" / f"{stamp}-{identifier}.md"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    arguments = parser().parse_args(argv)
    home = codex_home(arguments.codex_home)
    try:
        if arguments.command is None:
            if sys.stdin.isatty():
                return _interactive(home)
            parser().print_help()
            return 0
        if arguments.command == "list":
            if not 1 <= arguments.limit <= 1000:
                raise RepairError("--limit 需要在 1 到 1000 之间。")
            report = {"codex_home": str(home), "sessions": list_sessions(home, arguments.cwd, arguments.include_archived, arguments.limit)}
        elif arguments.command == "restore":
            report = restore_backup(arguments.backup, home, arguments.apply, arguments.offline)
        else:
            snapshot = load_snapshot(find_session(home, arguments.session))
            if arguments.command == "inspect":
                report = inspect_snapshot(snapshot, home)
            elif arguments.command == "repair":
                plan = make_plan(snapshot)
                report = apply_plan(plan, home, arguments.offline) if arguments.apply else plan.report()
            else:
                report = export_handoff(snapshot, arguments.output or _default_export(home, snapshot.session_id))
        _show(report, arguments.json)
        return 0
    except (RepairError, OSError, UnicodeError, RecursionError) as exc:
        message = str(exc) if isinstance(exc, RepairError) else "文件或编码操作失败；请检查路径、权限、磁盘空间和文件完整性。"
        if getattr(arguments, "json", False):
            print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        else:
            print("错误：" + message, file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("已取消。", file=sys.stderr)
        return 130
