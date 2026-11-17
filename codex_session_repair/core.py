"""Read local rollouts and make reversible, byte-offset-preserving repairs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import tomllib
import uuid
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from . import __version__

MAX_ROLLOUT_BYTES = 256 * 1024 * 1024
CIPHER_KEYS = {"encrypted_content", "encrypted_function_args"}
OPTIONAL_CIPHER_TYPES = {
    "message", "function_call", "custom_tool_call", "local_shell_call",
    "tool_search_call", "web_search_call", "image_generation_call",
}
OPAQUE_COMPACTION_TYPES = {"compaction", "context_compaction"}
AGENT_OMISSION = "[Encrypted agent payload omitted during backend migration.]"


class RepairError(Exception):
    """An actionable error whose message contains no history or credential values."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def codex_home(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _json(data: str | bytes) -> Any:
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _line_parts(raw: bytes) -> tuple[bytes, bytes]:
    if raw.endswith(b"\r\n"):
        return raw[:-2], b"\r\n"
    if raw.endswith(b"\n"):
        return raw[:-1], b"\n"
    return raw, b""


def parse_rollout(data: bytes) -> list[dict[str, Any]]:
    records = []
    # Split on LF only: offsets refer to bytes, including every original newline.
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    for number, raw in enumerate(lines, 1):
        try:
            record = _json(raw.decode("utf-8-sig" if number == 1 else "utf-8"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RepairError(f"JSONL 第 {number} 行无效；请先停止目标会话，保留文件并检查是否截断。") from exc
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise RepairError(f"JSONL 第 {number} 行不是支持的 Codex 记录。")
        if not isinstance(record.get("type"), str):
            raise RepairError(f"JSONL 第 {number} 行缺少记录类型。")
        records.append(record)
    if not records:
        raise RepairError("会话文件为空。")
    return records


def _validate_compaction(record: dict[str, Any], number: int) -> None:
    if record["type"] != "compacted":
        return
    payload = record["payload"]
    history = payload.get("replacement_history")
    metadata = payload.get("replacement_history_metadata")
    if history is not None and (not isinstance(history, list) or any(not isinstance(v, dict) for v in history)):
        raise RepairError(f"第 {number} 行的压缩历史格式未知；请使用 export 交接。")
    if metadata is not None and (
        not isinstance(metadata, list) or history is None or len(metadata) != len(history)
    ):
        raise RepairError(f"第 {number} 行的压缩历史与元数据长度不匹配，拒绝改写。")


@dataclass
class Snapshot:
    path: Path
    data: bytes
    records: list[dict[str, Any]]
    meta: dict[str, Any]

    @property
    def session_id(self) -> str:
        return self.meta["id"]


def load_snapshot(path: str | Path) -> Snapshot:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise RepairError("找不到会话文件。")
    if path.stat().st_size > MAX_ROLLOUT_BYTES:
        raise RepairError("会话文件超过 256 MiB，拒绝在内存中改写。请先单独导出所需历史。")
    data = path.read_bytes()
    records = parse_rollout(data)
    if records[0]["type"] != "session_meta":
        raise RepairError("第一行不是 session_meta，无法可靠确认目标会话。")
    meta = records[0]["payload"]
    try:
        identifier = str(uuid.UUID(meta["id"]))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RepairError("会话元数据缺少有效 UUID。") from exc
    if meta["id"] != identifier:
        raise RepairError("会话 UUID 不是规范格式。")
    for number, record in enumerate(records, 1):
        if record["type"] == "session_meta" and record["payload"].get("id") != identifier:
            raise RepairError("同一个文件包含不同会话的元数据，拒绝改写。")
        _validate_compaction(record, number)
    return Snapshot(path, data, records, meta)


def _normal_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return value


def _cwd_key(value: str) -> str:
    value = _normal_path(value).replace("\\", "/").rstrip("/")
    if os.name == "nt" or (len(value) > 1 and value[1] == ":") or value.startswith("//"):
        return value.casefold()
    return value


def _state_files(home: Path) -> list[Path]:
    return sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1, reverse=True)


def state_entry(home: Path, session_id: str) -> dict[str, Any]:
    for db in _state_files(home):
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as connection:
                available = {v[1] for v in connection.execute("PRAGMA table_info(threads)")}
                fields = [v for v in ("id", "rollout_path", "history_mode", "model_provider", "cli_version") if v in available]
                if "id" not in fields:
                    continue
                row = connection.execute("SELECT " + ",".join(fields) + " FROM threads WHERE id = ?", (session_id,)).fetchone()
                if row:
                    return dict(zip(fields, row))
        except sqlite3.Error:
            continue
    return {}


def _inside_sessions(path: Path, home: Path) -> bool:
    return any(path.is_relative_to((home / folder).resolve()) for folder in ("sessions", "archived_sessions"))


def find_session(home: Path, target: str) -> Path:
    explicit = Path(target).expanduser()
    if explicit.is_file():
        return explicit.resolve()
    try:
        identifier = str(uuid.UUID(target))
    except ValueError as exc:
        raise RepairError("请提供完整会话 UUID，或现有 JSONL 文件路径。") from exc
    indexed = state_entry(home, identifier).get("rollout_path")
    if isinstance(indexed, str):
        path = Path(_normal_path(indexed)).resolve()
        if path.is_file() and _inside_sessions(path, home):
            if load_snapshot(path).session_id == identifier:
                return path
            raise RepairError("数据库索引与文件中的会话 ID 不一致。")
    candidates = []
    for folder in ("sessions", "archived_sessions"):
        for path in (home / folder).rglob(f"*{identifier}*.jsonl"):
            if load_snapshot(path).session_id == identifier:
                candidates.append(path.resolve())
    if len(candidates) != 1:
        raise RepairError("没有找到唯一会话文件；请用 inspect 的完整文件路径明确选择。")
    return candidates[0]


def current_config(home: Path) -> dict[str, Any]:
    """Return a strict allowlist; never read auth.json or print provider headers."""
    path = home / "config.toml"
    if not path.exists():
        return {"source": str(path), "status": "missing"}
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        provider_id = config.get("model_provider", "openai")
        providers = config.get("model_providers", {})
        provider = providers.get(provider_id, {})
        if not isinstance(provider, dict):
            raise ValueError("invalid provider")
        endpoint = provider.get("base_url", "https://api.openai.com/v1" if provider_id == "openai" else "")
        override = os.environ.get("OPENAI_BASE_URL") if provider_id == "openai" else None
        if override:
            endpoint = override
        parsed = urlsplit(endpoint)
        # Credentials, query strings and fragments do not belong in a diagnostic.
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        safe_endpoint = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return {
            "source": str(path), "status": "ok", "model_provider": provider_id,
            "model": config.get("model"), "base_url": safe_endpoint,
            "wire_api": provider.get("wire_api", "responses"),
            "endpoint_source": "OPENAI_BASE_URL" if override else "config.toml",
            "historical_endpoint": "not_recorded_in_rollout",
        }
    except (ValueError, TypeError, AttributeError, OSError):
        return {"source": str(path), "status": "invalid", "error": "无法解析配置；未输出配置原文。"}


def cipher_paths(value: Any, path: str = "") -> Iterator[str]:
    """Find structured fields only, never JSON-looking strings in user/tool text."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + "/" + key
            if key in CIPHER_KEYS and child is not None:
                yield child_path
            else:
                yield from cipher_paths(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from cipher_paths(child, path + "/" + str(index))


def _failure_code(record: dict[str, Any]) -> bool:
    if record["type"] != "event_msg":
        return False
    payload = record["payload"]
    if payload.get("type") not in {"task_complete", "error"}:
        return False
    error = payload.get("error", payload if payload.get("type") == "error" else {})
    if not isinstance(error, dict):
        return False
    if error.get("code") == "invalid_encrypted_content":
        return True
    message = error.get("message", "")
    if not isinstance(message, str):
        return False
    try:
        inner = _json(message)
        return isinstance(inner, dict) and isinstance(inner.get("error"), dict) and inner["error"].get("code") == "invalid_encrypted_content"
    except (ValueError, RecursionError):
        return "invalid_encrypted_content" in message


def inspect_snapshot(snapshot: Snapshot, home: Path) -> dict[str, Any]:
    occurrences = []
    types: Counter[str] = Counter()
    for number, record in enumerate(snapshot.records, 1):
        if record["type"] == "response_item":
            types[str(record["payload"].get("type", "unknown"))] += 1
        for path in cipher_paths(record):
            occurrences.append({"line": number, "record_type": record["type"], "path": path})
    errors = [n for n, record in enumerate(snapshot.records, 1) if _failure_code(record)]
    state = state_entry(home, snapshot.session_id)
    return {
        "session_id": snapshot.session_id, "path": str(snapshot.path),
        "cwd": snapshot.meta.get("cwd"), "cli_version": snapshot.meta.get("cli_version"),
        "recorded_model_provider": snapshot.meta.get("model_provider"),
        "history_mode": state.get("history_mode", snapshot.meta.get("history_mode", "unknown")),
        "history_base": snapshot.meta.get("history_base"),
        "size_bytes": len(snapshot.data), "records": len(snapshot.records),
        "sha256": sha256(snapshot.data), "response_types": dict(types),
        "encrypted_fields": len(occurrences), "encrypted_locations": occurrences,
        "observed_error_lines": errors,
        "diagnosis": "observed_invalid_encrypted_content" if errors and occurrences else "historical_error_without_cipher" if errors else "encrypted_history_present" if occurrences else "no_encrypted_fields",
        "current_config": current_config(home),
    }


def list_sessions(home: Path, cwd: str | None = None, include_archived: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    paths = []
    for folder in ("sessions", "archived_sessions") if include_archived else ("sessions",):
        paths.extend((home / folder).rglob("*.jsonl"))
    result = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with path.open("rb") as stream:
                first = _json(stream.readline().decode("utf-8-sig"))
            meta = first.get("payload", {})
            if first.get("type") != "session_meta" or not isinstance(meta.get("id"), str):
                continue
            if isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
                continue
            if cwd and _cwd_key(str(meta.get("cwd", ""))) != _cwd_key(cwd):
                continue
            state = state_entry(home, meta["id"])
            indexed = state.get("rollout_path")
            if indexed and Path(_normal_path(indexed)).resolve() != path.resolve():
                continue
            result.append({
                "session_id": meta["id"], "path": str(path.resolve()), "cwd": meta.get("cwd"),
                "cli_version": meta.get("cli_version"), "model_provider": meta.get("model_provider"),
                "timestamp": meta.get("timestamp"), "archived": path.is_relative_to(home / "archived_sessions"),
                "size_bytes": path.stat().st_size,
            })
            if len(result) >= limit:
                break
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            continue
    return result


def _summary_text(item: dict[str, Any]) -> str:
    summary = item.get("summary", [])
    if not isinstance(summary, list):
        raise RepairError("推理摘要格式未知，拒绝改写。")
    return "\n".join(v["text"] for v in summary if isinstance(v, dict) and isinstance(v.get("text"), str))


def _sanitize_item(item: dict[str, Any], fallback_summary: str | None = None) -> tuple[dict[str, Any] | None, str]:
    kind = item.get("type")
    paths = list(cipher_paths(item))
    if not paths:
        return item, "unchanged"
    if kind == "reasoning":
        # Preserve its visible summary locally as an event, not a reasoning wire item.
        return None, "reasoning_removed_from_replay"
    if kind in OPAQUE_COMPACTION_TYPES:
        if not fallback_summary or not fallback_summary.strip():
            raise RepairError("存在仅有密文的压缩摘要，无法重建其含义。请使用 export 建立新会话，或恢复原后端。")
        return {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": fallback_summary}],
        }, "compaction_replaced_with_visible_summary"
    if kind == "agent_message":
        content = item.get("content")
        if not isinstance(content, list) or any(not isinstance(v, dict) for v in content):
            raise RepairError("代理消息格式未知，拒绝改写。")
        retained = []
        removed = False
        for part in content:
            if part.get("type") == "encrypted_content" and "encrypted_content" in part:
                removed = True
            else:
                retained.append(part)
        if not removed:
            raise RepairError("代理消息的密文字段位置未知，拒绝递归删除。")
        item["content"] = retained + [{"type": "input_text", "text": AGENT_OMISSION}]
        if list(cipher_paths(item)):
            raise RepairError("代理消息仍含未知密文字段，拒绝改写。")
        return item, "encrypted_agent_blocks_omitted"
    if kind in OPTIONAL_CIPHER_TYPES:
        for key in CIPHER_KEYS:
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                raise RepairError("密文字段不是字符串，格式未知。")
            item.pop(key, None)
        if list(cipher_paths(item)):
            raise RepairError("密文字段位于未知嵌套结构；请使用 export。")
        return item, "optional_cipher_fields_removed"
    raise RepairError("存在不支持的加密记录类型；请使用 export，保留原始文件以供适配。")


@dataclass
class Plan:
    snapshot: Snapshot
    repaired: bytes
    changes: list[dict[str, Any]]

    def report(self) -> dict[str, Any]:
        return {
            "session_id": self.snapshot.session_id, "path": str(self.snapshot.path),
            "mode": "preview", "changed_lines": len(self.changes), "changes": self.changes,
            "original_sha256": sha256(self.snapshot.data), "repaired_sha256": sha256(self.repaired),
            "original_bytes": len(self.snapshot.data), "repaired_bytes": len(self.repaired),
            "byte_offsets_preserved": True, "records_preserved": len(self.snapshot.records),
            "remaining_encrypted_fields": sum(len(list(cipher_paths(v))) for v in parse_rollout(self.repaired)),
        }


def make_plan(snapshot: Snapshot) -> Plan:
    if any(v["payload"].get("history_base") for v in snapshot.records if v["type"] == "session_meta"):
        raise RepairError("会话引用另一个不可变历史前缀，单文件修复不能覆盖它。请先 inspect 前缀会话或用 export 交接。")
    lines = snapshot.data.splitlines(keepends=True)
    if len(lines) != len(snapshot.records):
        raise RepairError("JSONL 含不支持的换行形式，拒绝改写。")
    result = []
    changes = []
    for number, (raw, original) in enumerate(zip(lines, snapshot.records), 1):
        paths = list(cipher_paths(original))
        if not paths:
            result.append(raw)
            continue
        record = copy.deepcopy(original)
        actions = []
        if record["type"] == "response_item":
            item, action = _sanitize_item(record["payload"])
            actions.append(action)
            if item is None:
                summary = _summary_text(record["payload"])
                record["type"] = "event_msg"
                record["payload"] = {"type": "agent_reasoning", "text": summary}
            else:
                record["payload"] = item
        elif record["type"] == "compacted":
            payload = record["payload"]
            history = payload.get("replacement_history")
            if not isinstance(history, list):
                raise RepairError(f"第 {number} 行没有可识别的压缩历史。")
            retained = []
            retained_metadata = []
            metadata = payload.get("replacement_history_metadata")
            summary = payload.get("message")
            for index, item in enumerate(history):
                replacement, action = _sanitize_item(item, summary if isinstance(summary, str) else None)
                if action != "unchanged":
                    actions.append(action)
                if replacement is not None:
                    retained.append(replacement)
                    if metadata is not None:
                        retained_metadata.append(metadata[index])
            payload["replacement_history"] = retained
            if metadata is not None:
                payload["replacement_history_metadata"] = retained_metadata
        else:
            raise RepairError(f"第 {number} 行的密文不在受支持的响应或压缩历史中，拒绝改写。")
        if list(cipher_paths(record)):
            raise RepairError(f"第 {number} 行仍有未处理的密文字段，拒绝改写。")
        body, ending = _line_parts(raw)
        bom = b"\xef\xbb\xbf" if number == 1 and body.startswith(b"\xef\xbb\xbf") else b""
        encoded = bom + _json_bytes(record)
        if len(encoded) > len(body):
            raise RepairError(f"第 {number} 行的修复内容超过原字节预算，无法保护分页偏移。请使用 export。")
        replacement_line = encoded + b" " * (len(body) - len(encoded)) + ending
        result.append(replacement_line)
        changes.append({"line": number, "actions": actions, "encrypted_fields": len(paths), "bytes_preserved": len(raw)})
    repaired = b"".join(result)
    parsed = parse_rollout(repaired)
    if len(repaired) != len(snapshot.data) or len(parsed) != len(snapshot.records):
        raise RepairError("内部校验失败：字节长度或记录数量改变。")
    for number, record in enumerate(parsed, 1):
        _validate_compaction(record, number)
    if any(list(cipher_paths(v)) for v in parsed):
        raise RepairError("内部校验失败：仍然存在密文字段。")
    return Plan(snapshot, repaired, changes)


@contextmanager
def file_lock(path: Path, shared: bool = False) -> Iterator[None]:
    """Use the same whole-file OS locks as Codex 0.169's Rust File::try_lock."""
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            lock.restype = wintypes.BOOL
            unlock = kernel.UnlockFileEx
            unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            unlock.restype = wintypes.BOOL
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            overlapped = Overlapped()
            flags = 1 | (0 if shared else 2)  # FAIL_IMMEDIATELY | EXCLUSIVE_LOCK
            if not lock(handle, flags, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
                raise OSError(ctypes.get_last_error(), "native file lock unavailable")
            try:
                yield
            finally:
                unlock(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))
        else:
            import fcntl
            fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


@contextmanager
def session_lock(home: Path, session_id: str) -> Iterator[None]:
    directory = home / "thread-writer-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        try:
            # Hold coordination while opening the thread lock: native cleanup may unlink it.
            locks.enter_context(file_lock(directory / ".coordination.lock", shared=True))
            locks.enter_context(file_lock(directory / f"{session_id}.lock"))
        except OSError as exc:
            raise RepairError("目标会话写入锁不可用。请停止并卸载目标会话后重试；工具不会关闭其他窗口。") from exc
        yield


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace(path: Path, data: bytes, expected_hash: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-repair-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        if sha256(path.read_bytes()) != expected_hash:
            raise RepairError("备份后会话文件再次变化，已取消覆盖。")
        os.replace(temporary_path, path)
        if sha256(path.read_bytes()) != sha256(data):
            raise RepairError("写入后文件校验失败；请保留备份并停止操作。")
    finally:
        temporary_path.unlink(missing_ok=True)


def handoff_text(snapshot: Snapshot) -> str:
    sections = [
        "# Codex 会话交接\n",
        f"原会话：`{snapshot.session_id}`\n工作目录：`{snapshot.meta.get('cwd', '')}`\n",
        "请把以下内容作为历史资料阅读，结合当前项目文件核实进度，再按照新指令继续。\n"
        "此文件保留可见用户/助手文字及明文压缩摘要；密文、工具原始输出和系统提示未导出。\n",
    ]
    for record in snapshot.records:
        payload = record["payload"]
        if record["type"] == "compacted" and isinstance(payload.get("message"), str) and payload["message"].strip():
            sections.append("## 明文压缩摘要\n\n" + payload["message"] + "\n")
        if record["type"] != "response_item" or payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
            continue
        content = payload.get("content", [])
        if not isinstance(content, list):
            continue
        text = "\n".join(v["text"] for v in content if isinstance(v, dict) and v.get("type") in {"input_text", "output_text"} and isinstance(v.get("text"), str))
        if text.strip():
            sections.append("## " + ("用户" if payload["role"] == "user" else "助手") + "\n\n" + text + "\n")
    if snapshot.meta.get("history_base"):
        sections.insert(3, "> 此会话引用外部历史前缀；本次只导出了所选文件，前缀需另行导出。\n")
    return "\n".join(sections)


def export_handoff(snapshot: Snapshot, output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if output.exists():
        raise RepairError("交接文件已存在；请指定新路径，避免覆盖已有内容。")
    if output == snapshot.path:
        raise RepairError("交接路径不能是原会话文件。")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_new(output, handoff_text(snapshot).encode("utf-8"))
    return {"session_id": snapshot.session_id, "output": str(output), "source_unchanged": True, "includes_external_history_base": False}


def _require_offline(offline: bool) -> None:
    if not offline:
        raise RepairError("实际写入需要 --offline：请先确认目标会话已停止并卸载。预览不需要此参数。")


def _check_target(snapshot: Snapshot, home: Path) -> None:
    if not _inside_sessions(snapshot.path, home):
        raise RepairError("实际修复仅允许所选 CODEX_HOME 的 sessions / archived_sessions 文件。")
    indexed = state_entry(home, snapshot.session_id).get("rollout_path")
    if indexed and Path(_normal_path(indexed)).resolve() != snapshot.path:
        raise RepairError("所选文件不是数据库当前指向的 rollout，拒绝修改旧前缀。")


def apply_plan(plan: Plan, home: Path, offline: bool = False) -> dict[str, Any]:
    _require_offline(offline)
    snapshot = plan.snapshot
    _check_target(snapshot, home)
    if not plan.changes:
        return {**plan.report(), "mode": "unchanged", "backup": None}
    with session_lock(home, snapshot.session_id):
        _check_target(snapshot, home)
        if snapshot.path.read_bytes() != snapshot.data:
            raise RepairError("会话文件已变化；请重新预览，工具没有改写。")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = home / "backups" / "codex-session-repair" / f"{stamp}-{snapshot.session_id}"
        backup.mkdir(parents=True, mode=0o700)
        original = backup / "original.jsonl"
        _write_new(original, snapshot.data)
        if sha256(original.read_bytes()) != sha256(snapshot.data):
            raise RepairError("备份校验失败，未修改会话。")
        _write_new(backup / "handoff.md", handoff_text(snapshot).encode("utf-8"))
        manifest = {
            "schema_version": 1, "tool_version": __version__, "session_id": snapshot.session_id,
            "source_path": str(snapshot.path), "codex_home": str(home),
            "original_sha256": sha256(snapshot.data), "repaired_sha256": sha256(plan.repaired),
            "size_bytes": len(snapshot.data), "changed_lines": len(plan.changes),
            "created_at": stamp, "byte_offsets_preserved": True,
        }
        _write_new(backup / "manifest.json", _json_bytes(manifest) + b"\n")
        _atomic_replace(snapshot.path, plan.repaired, sha256(snapshot.data))
    return {**plan.report(), "mode": "applied", "backup": str(backup), "handoff": str(backup / "handoff.md")}


def restore_backup(backup: Path, home: Path, apply: bool = False, offline: bool = False) -> dict[str, Any]:
    backup = backup.expanduser().resolve()
    try:
        manifest = _json((backup / "manifest.json").read_bytes())
        original_snapshot = load_snapshot(backup / "original.jsonl")
        original = original_snapshot.data
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unknown manifest")
        source = Path(manifest["source_path"]).resolve()
        snapshot = load_snapshot(source)
        if snapshot.session_id != manifest["session_id"] or original_snapshot.session_id != snapshot.session_id or sha256(original) != manifest["original_sha256"]:
            raise ValueError("mismatched backup")
        if len(original) != manifest["size_bytes"] or len(snapshot.data) != len(original):
            raise ValueError("changed length")
        parse_rollout(original)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RepairError("备份不完整、校验不匹配，或会话长度已变化；拒绝恢复覆盖。") from exc
    _check_target(snapshot, home)
    current_hash = sha256(snapshot.data)
    report = {"session_id": snapshot.session_id, "path": str(source), "backup": str(backup), "mode": "restore_preview"}
    if current_hash == manifest["original_sha256"]:
        return {**report, "mode": "already_original"}
    if current_hash != manifest["repaired_sha256"]:
        raise RepairError("修复后会话又有新写入，恢复会覆盖新历史，因此拒绝。请使用备份中的 handoff.md。")
    if not apply:
        return report
    _require_offline(offline)
    with session_lock(home, snapshot.session_id):
        _check_target(snapshot, home)
        _atomic_replace(source, original, current_hash)
    return {**report, "mode": "restored", "sha256": sha256(original)}
