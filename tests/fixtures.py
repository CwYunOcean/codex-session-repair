"""Synthetic histories only. No real session or credential belongs in tests."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SESSION_ID = "11111111-2222-4333-8444-555555555555"
CIPHER = "TEST_ONLY_OPAQUE_PROVIDER_A_" + "x" * 800


def message(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}


def record(kind, payload):
    return {"timestamp": "2026-11-17T10:00:00.000Z", "type": kind, "payload": payload}


def fixture_records(cwd):
    return [
        record("session_meta", {
            "id": SESSION_ID, "session_id": SESSION_ID, "timestamp": "2026-11-17T10:00:00Z",
            "cwd": str(cwd), "source": "cli", "originator": "codex_cli_rs",
            "cli_version": "0.169.0", "model_provider": "custom",
            "base_instructions": {"text": "Synthetic offline test."},
        }),
        record("response_item", message("user", '保留中文、路径 C:\\工程\\板子，以及示例 {"encrypted_content":"USER_LITERAL"}。')),
        record("response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "公开推理摘要"}], "encrypted_content": CIPHER}),
        record("response_item", {"type": "function_call", "name": "inspect_file", "arguments": '{"path":"板子.json"}', "call_id": "call_test"}),
        record("response_item", {"type": "function_call_output", "call_id": "call_test", "output": '结果示例：{"encrypted_content":"TOOL_LITERAL"}'}),
        record("response_item", {"type": "agent_message", "author": "/root/worker", "recipient": "/root", "content": [
            {"type": "input_text", "text": "可见代理前缀。"},
            {"type": "encrypted_content", "encrypted_content": CIPHER},
        ]}),
        record("compacted", {
            "message": "保留的中文压缩摘要。", "window_id": "window-test",
            "replacement_history": [
                message("user", "压缩后的可见用户内容"),
                {"type": "reasoning", "summary": [], "encrypted_content": CIPHER},
                {"type": "compaction", "encrypted_content": CIPHER},
            ],
            "replacement_history_metadata": [{"history_sequence": 10}, {"history_sequence": 11}, {"history_sequence": 12}],
            "retained_context": {"user_messages": [{"text": "保留的用户问题。", "order": 1}]},
        }),
        record("response_item", message("assistant", "已经检查原理图文件，继续之前请读当前文件。")),
        record("event_msg", {"type": "task_complete", "error": {
            "message": json.dumps({"error": {"code": "invalid_encrypted_content", "message": "Synthetic provider mismatch"}}),
        }}),
    ]


def write_fixture(home: Path, records=None, newline=b"\n", bom=False, final_newline=True):
    home.mkdir(parents=True, exist_ok=True)
    work = home / "work"
    work.mkdir(exist_ok=True)
    if records is None:
        records = fixture_records(work)
    target = home / "sessions/2026/11/17" / f"rollout-2026-11-17T10-00-00-{SESSION_ID}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(v, ensure_ascii=False).encode("utf-8") for v in records]
    data = (b"\xef\xbb\xbf" if bom else b"") + newline.join(lines) + (newline if final_newline else b"")
    target.write_bytes(data)
    return target, records


def add_pagination_indexes(home: Path, target: Path):
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, history_mode TEXT)")
        connection.execute("INSERT INTO threads VALUES (?, ?, 'paginated')", (SESSION_ID, str(target)))
    offsets = []
    start = 0
    for number, line in enumerate(target.read_bytes().splitlines(keepends=True)):
        offsets.append((SESSION_ID, number, start, start + len(line)))
        start += len(line)
    with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE thread_turns (thread_id TEXT, rollout_ordinal INTEGER, rollout_byte_offset INTEGER, rollout_end_byte_offset INTEGER)")
        connection.executemany("INSERT INTO thread_turns VALUES (?, ?, ?, ?)", offsets)
        connection.execute("CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER)")
        connection.execute("INSERT INTO thread_history_projection_state VALUES (?, ?, ?)", (SESSION_ID, start, len(offsets)))
    return offsets
