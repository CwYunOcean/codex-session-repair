"""Reproduce a backend-bound opaque-history failure entirely on localhost.

The fake tokens use HMAC only to model issuer isolation; they are NOT OpenAI's
encryption format. --codex also exercises an installed Codex client against
the fake Responses servers in a temporary, isolated CODEX_HOME.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_session_repair.core import RepairError, apply_plan, load_snapshot, make_plan, restore_backup
from tests.fixtures import fixture_records, write_fixture

SUCCESS = "LOCAL_REPLAY_OK"


class Backend:
    def __init__(self, name):
        self.name = name
        self.key = ("test-only-key-for-" + name).encode()
        self.requests = []
        self.statuses = []

    def issue(self):
        body = b"synthetic opaque reasoning payload"
        signature = hmac.new(self.key, body, hashlib.sha256).digest()
        return "TEST_ONLY:" + base64.urlsafe_b64encode(body + signature).decode()

    def accepts(self, token):
        try:
            prefix, encoded = token.split(":", 1)
            payload = base64.urlsafe_b64decode(encoded)
            return prefix == "TEST_ONLY" and hmac.compare_digest(payload[-32:], hmac.new(self.key, payload[:-32], hashlib.sha256).digest())
        except (ValueError, TypeError, AttributeError):
            return False

    def validate(self, items):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("encrypted_content", "encrypted_function_args"):
                if item.get(key) is not None and not self.accepts(item[key]):
                    return False
            if item.get("type") == "agent_message":
                for part in item.get("content", []):
                    if part.get("type") == "encrypted_content" and not self.accepts(part.get("encrypted_content")):
                        return False
        return True


@contextmanager
def serve(backend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            data = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            backend.requests.append(body)
            if not backend.validate(body.get("input", [])):
                backend.statuses.append(400)
                data = json.dumps({"error": {"type": "invalid_request_error", "code": "invalid_encrypted_content", "message": "Synthetic encrypted history belongs to a different backend."}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            backend.statuses.append(200)
            response_id = "resp_" + uuid.uuid4().hex
            reasoning = {"type": "reasoning", "id": "rs_" + uuid.uuid4().hex, "summary": [], "encrypted_content": backend.issue()}
            content = {"type": "output_text", "text": SUCCESS, "annotations": []}
            message = {"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant", "status": "completed", "content": [content]}
            response = {
                "id": response_id, "object": "response", "model": "gpt-5.1-codex", "status": "completed",
                "output": [reasoning, message],
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
            }
            if not body.get("stream"):
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
                {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
                {"type": "response.output_item.added", "output_index": 1, "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": message["id"], "output_index": 1, "content_index": 0, "part": {**content, "text": ""}},
                {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 1, "content_index": 0, "delta": SUCCESS},
                {"type": "response.output_text.done", "item_id": message["id"], "output_index": 1, "content_index": 0, "text": SUCCESS},
                {"type": "response.content_part.done", "item_id": message["id"], "output_index": 1, "content_index": 0, "part": content},
                {"type": "response.output_item.done", "output_index": 1, "item": message},
                {"type": "response.completed", "response": response},
            ]
            data = b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode() for e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def replay_items(snapshot):
    result = []
    for entry in snapshot.records:
        if entry["type"] == "response_item":
            result.append(entry["payload"])
        elif entry["type"] == "compacted" and entry["payload"].get("replacement_history") is not None:
            result = list(entry["payload"]["replacement_history"])
    return result


def request_status(url, items):
    request = urllib.request.Request(url + "/responses", data=json.dumps({"model": "test-model", "input": items}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as response:
        assert json.load(response)["error"]["code"] == "invalid_encrypted_content"
        return response.code


def simple_reproduction(home, url_a, url_b, backend_a):
    records = fixture_records(home / "work")
    records[2]["payload"]["encrypted_content"] = backend_a.issue()
    records[5]["payload"]["content"][1]["encrypted_content"] = backend_a.issue()
    records[6]["payload"]["replacement_history"][1]["encrypted_content"] = backend_a.issue()
    records[6]["payload"]["replacement_history"][2]["encrypted_content"] = backend_a.issue()
    path, _ = write_fixture(home, records)
    original = path.read_bytes()
    assert request_status(url_a, replay_items(load_snapshot(path))) == 200
    before = request_status(url_b, replay_items(load_snapshot(path)))
    result = apply_plan(make_plan(load_snapshot(path)), home, offline=True)
    after = request_status(url_b, replay_items(load_snapshot(path)))
    assert (before, after) == (400, 200)
    assert [len(v) for v in original.splitlines(keepends=True)] == [len(v) for v in path.read_bytes().splitlines(keepends=True)]
    assert make_plan(load_snapshot(path)).repaired == path.read_bytes()
    restore_backup(Path(result["backup"]), home, apply=True, offline=True)
    assert path.read_bytes() == original
    return {"before_switch_on_A": 200, "after_switch_on_B": before, "after_repair_on_B": after, "byte_boundaries_preserved": True, "restore_exact_bytes": True}


def _write_native_config(home, url):
    text = f'''model_provider = "custom"
model = "gpt-5.1-codex"
model_reasoning_effort = "low"
check_for_update_on_startup = false
project_root_markers = [".codex-repair-test-root"]
[model_providers.custom]
name = "Local reproduction only"
base_url = "{url}"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
env_key = "CODEX_SESSION_REPAIR_TEST_KEY"
[analytics]
enabled = false
'''
    (home / "config.toml").write_text(text, encoding="utf-8")


def native_reproduction(home, work, executable, url_a, url_b, backend_b):
    home.mkdir()
    work.mkdir()
    (work / ".codex-repair-test-root").touch()
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["CODEX_SESSION_REPAIR_TEST_KEY"] = "TEST_ONLY_NOT_A_REAL_API_KEY"
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment["PYTHONUTF8"] = "1"
    common = [executable, "--no-daemon", "-a", "never", "-s", "read-only", "exec"]
    flags = ["--skip-git-repo-check", "--ignore-rules", "--json", "--trust"]

    def run(arguments):
        return subprocess.run(arguments, cwd=work, env=environment, input="", capture_output=True, encoding="utf-8", errors="replace", timeout=45)

    _write_native_config(home, url_a)
    first = run(common + flags + ["Synthetic localhost test. Reply LOCAL_REPLAY_OK."])
    if first.returncode:
        raise RuntimeError("Native Codex bootstrap failed: " + first.stderr[-1500:] + first.stdout[-2500:])
    path = next((home / "sessions").rglob("*.jsonl"))
    snapshot = load_snapshot(path)
    if not make_plan(snapshot).changes:
        raise RuntimeError("Native client did not persist the fake encrypted reasoning item.")
    identifier = snapshot.session_id
    _write_native_config(home, url_b)
    before_count = len(backend_b.statuses)
    broken = run(common + ["resume"] + flags + [identifier, "Synthetic recovery test. Reply LOCAL_REPLAY_OK."])
    if broken.returncode == 0 or 400 not in backend_b.statuses[before_count:]:
        raise RuntimeError("Native client did not reproduce the expected HTTP 400: " + broken.stderr[-2000:])
    after_failed_turn = load_snapshot(path)
    original_at_repair = after_failed_turn.data
    result = apply_plan(make_plan(after_failed_turn), home, offline=True)
    repaired_at_resume = path.read_bytes()
    assert len(repaired_at_resume) == len(original_at_repair)
    before_count = len(backend_b.statuses)
    recovered = run(common + ["resume"] + flags + [identifier, "Synthetic recovery test. Reply LOCAL_REPLAY_OK."])
    if recovered.returncode or SUCCESS not in recovered.stdout or 200 not in backend_b.statuses[before_count:]:
        raise RuntimeError("Native replay after repair failed: " + recovered.stderr[-2500:])
    captured = backend_b.requests[-1].get("input", [])
    assert backend_b.validate(captured)
    assert not any(v.get("type") == "reasoning" for v in captured)
    try:
        restore_backup(Path(result["backup"]), home, apply=True, offline=True)
    except RepairError:
        pass
    else:
        raise AssertionError("Restore must refuse to erase the newly successful turn.")
    return {
        "client_version": run([executable, "--version"]).stdout.strip(),
        "backend_id_reused": "custom", "before_repair": 400, "after_repair": 200,
        "native_resume_success": True, "same_session_id": identifier,
        "new_turn_protected_from_restore": True,
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", nargs="?", const="codex", metavar="EXECUTABLE", help="同时测试本机 Codex；只使用临时会话和本地模拟 API")
    arguments = parser.parse_args()
    backend_a, backend_b = Backend("A"), Backend("B")
    with tempfile.TemporaryDirectory(prefix="codex-repair-demo-") as temporary, serve(backend_a) as url_a, serve(backend_b) as url_b:
        root = Path(temporary)
        report = {"simulation": simple_reproduction(root / "simple-home", url_a, url_b, backend_a)}
        if arguments.codex:
            executable = shutil.which(arguments.codex) or arguments.codex
            report["native_codex"] = native_reproduction(root / "native-home", root / "work", executable, url_a, url_b, backend_b)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
