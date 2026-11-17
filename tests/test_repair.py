import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_session_repair.core import (
    AGENT_OMISSION, RepairError, apply_plan, cipher_paths, current_config,
    export_handoff, file_lock, find_session, handoff_text, inspect_snapshot,
    list_sessions, load_snapshot, make_plan, restore_backup, sha256,
)
from tests.fixtures import CIPHER, SESSION_ID, add_pagination_indexes, fixture_records, message, record, write_fixture


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "中文 home"
        self.path, self.records = write_fixture(self.home)

    def snapshot(self):
        return load_snapshot(self.path)

    def test_visible_messages_and_tool_io_are_byte_identical(self):
        original = self.path.read_bytes().splitlines(keepends=True)
        repaired = make_plan(self.snapshot()).repaired.splitlines(keepends=True)
        for index in (0, 1, 3, 4, 7, 8):
            self.assertEqual(original[index], repaired[index])
        self.assertIn(b"USER_LITERAL", b"".join(repaired))
        self.assertIn(b"TOOL_LITERAL", b"".join(repaired))

    def test_every_record_byte_boundary_and_database_reference_survives(self):
        offsets = add_pagination_indexes(self.home, self.path)
        before_db = {p.name: p.read_bytes() for p in self.home.glob("*.sqlite")}
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        self.assertEqual(result["mode"], "applied")
        data = self.path.read_bytes()
        for _, _, start, end in offsets:
            parsed = json.loads(data[start:end])
            self.assertIsInstance(parsed, dict)
            self.assertEqual(len(data[start:end]), end - start)
        for p in self.home.glob("*.sqlite"):
            self.assertEqual(before_db[p.name], p.read_bytes())
        with closing(sqlite3.connect(self.home / "thread_history_1.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state").fetchone()[0], len(data))

    def test_cipher_fields_are_removed_and_visible_reasoning_summary_is_local(self):
        plan = make_plan(self.snapshot())
        self.assertNotIn(CIPHER.encode(), plan.repaired)
        repaired = json.loads(plan.repaired.splitlines()[2])
        self.assertEqual(repaired["type"], "event_msg")
        self.assertEqual(repaired["payload"], {"type": "agent_reasoning", "text": "公开推理摘要"})
        self.assertEqual(plan.report()["remaining_encrypted_fields"], 0)

    def test_encrypted_agent_content_keeps_plaintext_and_marks_loss(self):
        repaired = json.loads(make_plan(self.snapshot()).repaired.splitlines()[5])
        self.assertEqual(repaired["payload"]["content"][0]["text"], "可见代理前缀。")
        self.assertEqual(repaired["payload"]["content"][-1]["text"], AGENT_OMISSION)

    def test_compaction_retains_summary_and_metadata_alignment(self):
        repaired = json.loads(make_plan(self.snapshot()).repaired.splitlines()[6])["payload"]
        self.assertEqual(len(repaired["replacement_history"]), 2)
        self.assertEqual(repaired["replacement_history_metadata"], [{"history_sequence": 10}, {"history_sequence": 12}])
        self.assertEqual(repaired["replacement_history"][1]["content"][0]["text"], self.records[6]["payload"]["message"])
        self.assertEqual(repaired["window_id"], "window-test")
        self.assertEqual(repaired["retained_context"], self.records[6]["payload"]["retained_context"])

    def test_repeated_repair_is_a_noop(self):
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        backup_count = len(list((self.home / "backups").rglob("manifest.json")))
        second = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        self.assertEqual(second["mode"], "unchanged")
        self.assertEqual(second["backup"], None)
        self.assertEqual(backup_count, len(list((self.home / "backups").rglob("manifest.json"))))
        self.assertTrue(Path(result["backup"]).is_dir())

    def test_backup_restores_the_exact_original_bytes(self):
        original = self.path.read_bytes()
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        backup = Path(result["backup"])
        self.assertEqual((backup / "original.jsonl").read_bytes(), original)
        before_restore = self.path.read_bytes()
        self.assertEqual(restore_backup(backup, self.home)["mode"], "restore_preview")
        self.assertEqual(self.path.read_bytes(), before_restore)
        self.assertEqual(restore_backup(backup, self.home, apply=True, offline=True)["mode"], "restored")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(restore_backup(backup, self.home, apply=True, offline=True)["mode"], "already_original")

    def test_restore_refuses_to_erase_new_turns(self):
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        with self.path.open("ab") as stream:
            stream.write(json.dumps(record("response_item", message("user", "新消息")), ensure_ascii=False).encode() + b"\n")
        current = self.path.read_bytes()
        with self.assertRaises(RepairError):
            restore_backup(Path(result["backup"]), self.home, apply=True, offline=True)
        self.assertEqual(self.path.read_bytes(), current)

    def test_restore_refuses_same_length_edits(self):
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        data = self.path.read_bytes().replace(b"USER_LITERAL", b"EDIT_LITERAL")
        self.path.write_bytes(data)
        with self.assertRaises(RepairError):
            restore_backup(Path(result["backup"]), self.home, apply=True, offline=True)
        self.assertEqual(self.path.read_bytes(), data)

    def test_tampered_backup_is_never_restored(self):
        result = apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        backup = Path(result["backup"])
        original = backup / "original.jsonl"
        original.write_bytes(original.read_bytes().replace(b"USER_LITERAL", b"EDIT_LITERAL"))
        with self.assertRaises(RepairError):
            restore_backup(backup, self.home, apply=True, offline=True)

    def test_preview_does_not_write_to_codex_home(self):
        before_files = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(make_plan(self.snapshot()).report()["mode"], "preview")
        inspect_snapshot(self.snapshot(), self.home)
        after_files = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before_files, after_files)

    def test_apply_requires_explicit_offline_assertion(self):
        with self.assertRaises(RepairError):
            apply_plan(make_plan(self.snapshot()), self.home)
        self.assertFalse((self.home / "backups").exists())

    def test_file_changed_since_preview_is_not_overwritten(self):
        plan = make_plan(self.snapshot())
        changed = self.path.read_bytes() + b"\n"
        self.path.write_bytes(changed)
        with self.assertRaises(RepairError):
            apply_plan(plan, self.home, offline=True)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertFalse((self.home / "backups").exists())

    def test_atomic_replace_failure_leaves_original_and_verified_backup(self):
        original = self.path.read_bytes()
        with patch("codex_session_repair.core.os.replace", side_effect=PermissionError("test-only")):
            with self.assertRaises((RepairError, OSError)):
                apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(list((self.home / "backups").rglob("original.jsonl"))), 1)
        self.assertFalse(list(self.path.parent.glob(".codex-repair-*.tmp")))

    def test_backup_write_failure_cannot_modify_original(self):
        original = self.path.read_bytes()
        with patch("codex_session_repair.core._write_new", side_effect=OSError("disk full")):
            with self.assertRaises((RepairError, OSError)):
                apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        self.assertEqual(original, self.path.read_bytes())

    def test_crlf_bom_and_missing_final_newline_survive(self):
        self.path, _ = write_fixture(self.home, newline=b"\r\n", bom=True, final_newline=False)
        original = self.path.read_bytes()
        repaired = make_plan(self.snapshot()).repaired
        self.assertTrue(repaired.startswith(b"\xef\xbb\xbf"))
        self.assertFalse(repaired.endswith(b"\n"))
        self.assertEqual(original.count(b"\r\n"), repaired.count(b"\r\n"))
        self.assertEqual([len(v) for v in original.splitlines(keepends=True)], [len(v) for v in repaired.splitlines(keepends=True)])

    def test_invalid_json_duplicate_keys_and_nonfinite_values_fail_before_writing(self):
        for bad in (b'{"type":', b'{"type":"session_meta","payload":{},"payload":{}}', b'{"type":"event_msg","payload":{"x":NaN}}', b'\xff'):
            with self.subTest(bad=bad):
                self.path.write_bytes(bad + b"\n")
                with self.assertRaises(RepairError):
                    load_snapshot(self.path)
                self.assertFalse((self.home / "backups").exists())

    def test_mismatched_compaction_metadata_is_rejected(self):
        self.records[6]["payload"]["replacement_history_metadata"].pop()
        write_fixture(self.home, self.records)
        with self.assertRaises(RepairError):
            self.snapshot()

    def test_opaque_compaction_without_plaintext_is_not_fake_decrypted(self):
        self.records[6]["payload"]["message"] = ""
        write_fixture(self.home, self.records)
        with self.assertRaises(RepairError):
            make_plan(self.snapshot())
        self.assertIn("USER_LITERAL", handoff_text(self.snapshot()))

    def test_unknown_encrypted_type_is_rejected_without_leaking_its_value(self):
        self.records.append(record("response_item", {"type": "future_item", "encrypted_content": "SECRET_DO_NOT_PRINT"}))
        write_fixture(self.home, self.records)
        with self.assertRaises(RepairError) as error:
            make_plan(self.snapshot())
        self.assertNotIn("SECRET_DO_NOT_PRINT", str(error.exception))

    def test_cipher_like_user_and_tool_strings_are_never_parsed(self):
        self.assertEqual(list(cipher_paths({"text": '{"encrypted_content":"opaque"}'})), [])

    def test_nested_unknown_cipher_is_not_silently_deleted(self):
        self.records[3]["payload"]["future"] = {"encrypted_content": CIPHER}
        write_fixture(self.home, self.records)
        with self.assertRaises(RepairError):
            make_plan(self.snapshot())

    def test_optional_function_cipher_keeps_arguments_and_outputs(self):
        self.records[3]["payload"]["encrypted_content"] = CIPHER
        self.records[3]["payload"]["encrypted_function_args"] = CIPHER
        write_fixture(self.home, self.records)
        item = json.loads(make_plan(self.snapshot()).repaired.splitlines()[3])["payload"]
        self.assertEqual(item["arguments"], self.records[3]["payload"]["arguments"])
        self.assertEqual(item["call_id"], "call_test")
        self.assertNotIn("encrypted_function_args", item)

    def test_inspect_counts_actual_errors_not_quoted_user_errors(self):
        self.records[1]["payload"] = message("user", "invalid_encrypted_content 是我的问题")
        write_fixture(self.home, self.records)
        report = inspect_snapshot(self.snapshot(), self.home)
        self.assertEqual(report["observed_error_lines"], [9])
        self.assertEqual(report["encrypted_fields"], 4)
        self.assertNotIn(CIPHER, json.dumps(report))

    def test_encryption_presence_alone_does_not_claim_a_broken_thread(self):
        write_fixture(self.home, self.records[:-1])
        self.assertEqual(inspect_snapshot(self.snapshot(), self.home)["diagnosis"], "encrypted_history_present")

    def test_original_error_log_is_preserved_but_not_reported_as_current_cipher(self):
        apply_plan(make_plan(self.snapshot()), self.home, offline=True)
        report = inspect_snapshot(self.snapshot(), self.home)
        self.assertEqual(report["diagnosis"], "historical_error_without_cipher")
        self.assertEqual(report["observed_error_lines"], [9])

    def test_inherited_history_is_rejected_and_export_marks_missing_prefix(self):
        self.records[0]["payload"]["history_base"] = {"thread_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "end_ordinal_exclusive": 100, "end_byte_offset": 1024}
        write_fixture(self.home, self.records)
        with self.assertRaises(RepairError):
            make_plan(self.snapshot())
        self.assertIn("前缀需另行导出", handoff_text(self.snapshot()))

    def test_patch_that_cannot_fit_original_byte_budget_is_rejected(self):
        records = fixture_records(self.home / "work")[:1]
        records.append({"type": "response_item", "payload": {"type": "agent_message", "content": [{"type": "encrypted_content", "encrypted_content": ""}]}})
        write_fixture(self.home, records)
        with self.assertRaises(RepairError):
            make_plan(self.snapshot())

    def test_export_is_readable_and_does_not_overwrite_existing_files(self):
        before = self.path.read_bytes()
        destination = self.home / "exports/交接.md"
        export_handoff(self.snapshot(), destination)
        exported = destination.read_text(encoding="utf-8")
        self.assertIn("保留中文", exported)
        self.assertIn("压缩摘要", exported)
        self.assertNotIn(CIPHER, exported)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(RepairError):
            export_handoff(self.snapshot(), destination)

    def test_config_allowlist_hides_credentials_query_and_headers(self):
        (self.home / "config.toml").write_text('''model_provider = "custom"
model = "test-model"
[model_providers.custom]
base_url = "https://USERNAME:PASSWORD@example.test/v1?key=SECRET_QUERY#SECRET_FRAGMENT"
experimental_bearer_token = "SECRET_BEARER"
http_headers = { Authorization = "SECRET_HEADER" }
''', encoding="utf-8")
        result = current_config(self.home)
        self.assertEqual(result["base_url"], "https://example.test/v1")
        output = json.dumps(result)
        for secret in ("USERNAME", "PASSWORD", "SECRET_QUERY", "SECRET_FRAGMENT", "SECRET_BEARER", "SECRET_HEADER"):
            self.assertNotIn(secret, output)

    def test_missing_or_invalid_config_does_not_block_offline_repair(self):
        self.assertEqual(current_config(self.home)["status"], "missing")
        (self.home / "config.toml").write_text('token = "NEVER_PRINT\n', encoding="utf-8")
        result = current_config(self.home)
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("NEVER_PRINT", json.dumps(result))
        self.assertTrue(make_plan(self.snapshot()).changes)

    def test_find_uses_current_database_rollout_after_revert(self):
        new_path = self.path.with_name(self.path.stem + "_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.jsonl")
        new_path.write_bytes(self.path.read_bytes())
        add_pagination_indexes(self.home, new_path)
        self.assertTrue(find_session(self.home, SESSION_ID).samefile(new_path))
        with self.assertRaises(RepairError):
            apply_plan(make_plan(self.snapshot()), self.home, offline=True)

    def test_ambiguous_session_files_require_explicit_selection(self):
        self.path.with_name("copy-" + SESSION_ID + ".jsonl").write_bytes(self.path.read_bytes())
        with self.assertRaises(RepairError):
            find_session(self.home, SESSION_ID)
        self.assertTrue(find_session(self.home, str(self.path)).samefile(self.path))

    def test_apply_rejects_file_outside_selected_home(self):
        other_home = Path(self.temporary.name) / "other"
        other_path, _ = write_fixture(other_home)
        with self.assertRaises(RepairError):
            apply_plan(make_plan(load_snapshot(other_path)), self.home, offline=True)

    def test_list_matches_windows_prefix_case_and_excludes_subagents(self):
        self.records[0]["payload"]["cwd"] = "\\\\?\\F:\\JLC"
        write_fixture(self.home, self.records)
        self.assertEqual(len(list_sessions(self.home, cwd="f:/jlc")), 1)
        self.records[0]["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "test"}}}
        write_fixture(self.home, self.records)
        self.assertEqual(list_sessions(self.home), [])

    def test_archived_sessions_need_explicit_listing_flag(self):
        archived = self.home / "archived_sessions" / self.path.name
        archived.parent.mkdir()
        self.path.rename(archived)
        self.assertEqual(list_sessions(self.home), [])
        self.assertEqual(len(list_sessions(self.home, include_archived=True)), 1)
        self.assertTrue(find_session(self.home, SESSION_ID).samefile(archived))

    def test_real_cross_process_native_lock_prevents_writes(self):
        lock_path = self.home / "thread-writer-locks" / (SESSION_ID + ".lock")
        lock_path.parent.mkdir()
        script = "from pathlib import Path\nfrom codex_session_repair.core import file_lock\nimport sys\nwith file_lock(Path(sys.argv[1])):\n print('locked', flush=True)\n sys.stdin.readline()\n"
        process = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(RepairError):
                apply_plan(make_plan(self.snapshot()), self.home, offline=True)
            self.assertFalse((self.home / "backups").exists())
        finally:
            process.communicate("release\n", timeout=10)
        self.assertEqual(process.returncode, 0)

    def test_cli_json_preview_and_restore_have_stable_exit_codes(self):
        command = [sys.executable, "-m", "codex_session_repair", "--codex-home", str(self.home)]
        original = self.path.read_bytes()
        preview = subprocess.run(command + ["repair", SESSION_ID, "--json"], capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(json.loads(preview.stdout)["mode"], "preview")
        self.assertEqual(self.path.read_bytes(), original)
        rejected = subprocess.run(command + ["repair", SESSION_ID, "--apply", "--json"], capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("error", json.loads(rejected.stderr))


if __name__ == "__main__":
    unittest.main()
