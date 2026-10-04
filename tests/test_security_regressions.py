"""Security regressions with isolated state, fake RPC peers and disposable children."""
import base64
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("guard_security", str(ROOT / "bin/codex-guard"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)


def healthy(used=10, weekly=None):
    limits = {"primary": {"windowDurationMins": 300, "usedPercent": used, "resetsAt": int(time.time()) + 900}}
    if weekly is not None:
        limits["secondary"] = {"windowDurationMins": 10080, "usedPercent": weekly}
    return {"rateLimits": limits}


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attribute, value in [("SNAPSHOT_FILE", str(Path(self.tmp.name) / "snapshot.json")),
                                 ("LOG_FILE", str(Path(self.tmp.name) / "guard.log")),
                                 ("CODEX_HOME", self.tmp.name)]:
            item = patch.object(cg, attribute, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(cg, "log")
        item.start()
        self.addCleanup(item.stop)


class QuotaTests(IsolatedTest):
    def test_missing_malformed_and_nonfinite_are_not_healthy(self):
        inputs = [None, {}, {"rateLimits": None}, {"rateLimits": []}, {"rateLimits": {"credits": None}},
                  {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 10080}}}]
        inputs += [healthy(value) for value in [None, "0", True, float("nan"), float("inf"), -1, 101]]
        for data in inputs:
            with self.subTest(data=data):
                snap = cg.QuotaSnapshot(data)
                self.assertFalse(snap.valid)
                self.assertEqual(snap.remaining_percent, 0)

    def test_secondary_five_hour_window_and_null_credits(self):
        data = healthy(10, 40)
        rl = data["rateLimits"]
        rl["primary"], rl["secondary"] = rl["secondary"], rl["primary"]
        rl["credits"] = None
        snap = cg.QuotaSnapshot(data)
        self.assertTrue(snap.valid)
        self.assertEqual(snap.remaining_percent, 90)
        self.assertEqual(snap.protection_remaining, 60)

    def test_weekly_limit_blocks_even_with_healthy_five_hours(self):
        snap = cg.QuotaSnapshot(healthy(10, 100))
        self.assertTrue(snap.valid)
        self.assertEqual(snap.protection_remaining, 0)

    def test_invalid_secondary_does_not_enable_recovery(self):
        self.assertFalse(cg.QuotaSnapshot(healthy(0, float("nan"))).valid)

    def test_large_reset_timestamp_does_not_crash_ui(self):
        data = healthy()
        data["rateLimits"]["primary"]["resetsAt"] = 10 ** 30
        self.assertEqual(cg.QuotaSnapshot(data).reset_datetime_str, "未知")


class ProcessAndStateTests(IsolatedTest):
    def test_prompts_and_unrelated_executables_are_never_monitors(self):
        for parts in [["codex", "fix codex-top and codex-guard watch"],
                      ["bash", "-c", "codex-guard watch"], ["rg", "codex-top"],
                      ["python3", "analyse.py", "codex-top"]]:
            self.assertFalse(cg.ProcessManager.is_guard_monitor(parts))
        self.assertTrue(cg.ProcessManager.is_guard_monitor(["python3", "/tmp/codex-guard", "watch"]))
        self.assertTrue(cg.ProcessManager.is_guard_monitor(["/tmp/codex-top"]))

    def test_exact_client_matching_and_global_options(self):
        for parts in [["my-codex-backup"], ["rg", "codex"], ["bash", "codex"],
                      ["codex", "-c", 'x="value"', "app-server"], ["codex", "queue"],
                      ["codex", "--version"], ["node", "codex-helper.js"]]:
            self.assertFalse(cg.ProcessManager.is_codex_client(parts), parts)
        for parts in [["codex", "-c", 'x="app-server"', "resume", "abc"],
                      ["node", "/tmp/codex.js", "resume", "abc"],
                      ["codex", "implement codex-top and daemon"], ["codex", "--", "--help"]]:
            self.assertTrue(cg.ProcessManager.is_codex_client(parts), parts)

    def test_proc_stat_handles_spaces_and_parentheses(self):
        content = "123 (codex (test) name) S 1 " + " ".join(str(i) for i in range(5, 53))
        with patch("builtins.open", unittest.mock.mock_open(read_data=content)):
            fields = cg.ProcessManager.read_stat(123)
        self.assertEqual(fields[0], "S")
        self.assertEqual(fields[19], "22")

    def test_signals_reject_reused_identity_and_nonpositive_pids(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(lambda: proc.poll() is None and proc.wait(timeout=5))
        identity = cg.ProcessManager.process_identity(proc.pid)
        wrong = dict(identity, start_time="different")
        self.assertFalse(cg.ProcessManager.signal_pid(proc.pid, signal.SIGTERM, wrong))
        self.assertIsNone(proc.poll())
        for pid in [0, -1, 1, True, os.getpid()]:
            self.assertFalse(cg.ProcessManager.signal_pid(pid, signal.SIGTERM))
        self.assertTrue(cg.ProcessManager.signal_pid(proc.pid, signal.SIGTERM, identity))
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)

    def test_snapshot_is_private_atomic_and_does_not_follow_symlink(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("unchanged")
        Path(cg.SNAPSHOT_FILE).symlink_to(victim)
        cg.write_snapshot({"reason": "quota_exceeded"})
        self.assertEqual(victim.read_text(), "unchanged")
        self.assertFalse(Path(cg.SNAPSHOT_FILE).is_symlink())
        self.assertEqual(os.stat(cg.SNAPSHOT_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(cg.read_snapshot()["reason"], "quota_exceeded")

    def test_manual_intent_cannot_be_overwritten_by_automatic_writer(self):
        cg.write_snapshot({"reason": "manual_pause"})
        self.assertFalse(cg.write_snapshot({"reason": "quota_recovered"}, automatic=True))
        self.assertEqual(cg.read_snapshot()["reason"], "manual_pause")

    def test_corrupt_snapshot_is_not_a_recovery_instruction(self):
        Path(cg.SNAPSHOT_FILE).write_text("[]")
        self.assertEqual(cg.read_snapshot(), {})
        for malformed in [{"active_pids": [0]}, {"sessions": [None]},
                          {"loaded_thread_ids": "wrong"}, {"process_identities": []}]:
            Path(cg.SNAPSHOT_FILE).write_text(json.dumps(malformed))
            self.assertEqual(cg.read_snapshot(), {})
        Path(cg.SNAPSHOT_FILE).write_text('{"broken"')
        self.assertEqual(cg.read_snapshot(), {})

    def test_singleton_monitor(self):
        with cg.monitor_lock():
            with self.assertRaises(SystemExit):
                with cg.monitor_lock():
                    self.fail("Second monitor acquired lock")
        with cg.monitor_lock():
            pass

    def test_lock_files_are_never_removed_or_used_as_loaded_sessions(self):
        directory = Path(self.tmp.name) / "locks"
        directory.mkdir()
        lock = directory / "old-thread.lock"
        lock.touch()
        with patch.object(cg.SessionManager, "LOCK_DIR", str(directory)), \
             patch.object(cg.CodexSocketClient, "query_loaded_threads", return_value=[]), \
             patch("subprocess.run") as run:
            cg.SessionManager.clean_stale_locks()
            self.assertEqual(cg.SessionManager.get_loaded_thread_ids(), [])
            run.assert_not_called()
        self.assertTrue(lock.exists())

    def test_readonly_database_with_uri_metacharacters(self):
        db = Path(self.tmp.name) / "state?#.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE test (x)")
        with patch.object(cg.SessionManager, "DB_PATH", str(db)):
            conn = cg.SessionManager.get_db_connection()
            self.assertIsNotNone(conn)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO test VALUES (1)")
            conn.close()
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("no access")) as connect, \
             patch.object(cg.SessionManager, "DB_PATH", str(db)):
            self.assertIsNone(cg.SessionManager.get_db_connection())
            self.assertEqual(connect.call_count, 1)

    def test_parent_cycles_terminate(self):
        def info(tid):
            return {"source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "b" if tid == "a" else "a"}}})}
        with patch.object(cg.SessionManager, "get_thread_info", side_effect=info):
            self.assertIn(cg.SessionManager.get_root_thread_id("a"), {"a", "b"})


class WatchTests(IsolatedTest):
    def simulate(self, data, clients, **kwargs):
        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=data), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=clients), \
             patch.object(cg.ProcessManager, "suspend_pid", return_value=True) as suspend, \
             patch.object(cg.ProcessManager, "resume_pid", return_value=True) as resume, \
             patch.object(cg.SessionManager, "interrupt_active_turns", return_value=[]), \
             patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=[]), \
             patch.object(cg.SessionManager, "launch_codex_session", return_value=False) as launch, \
             patch.object(cg.SessionManager, "restore_and_verify_sessions", return_value={"sessions": []}), \
             patch.object(cg, "send_notification"), patch("time.sleep", side_effect=KeyboardInterrupt):
            cg.run_watch_loop(interval=1, auto_continue=False, **kwargs)
        return suspend, resume, launch

    def test_unknown_quota_pauses_and_exit_does_not_unfreeze(self):
        suspend, resume, _ = self.simulate({"rateLimits": {}}, [(55555, "S", "codex")])
        suspend.assert_called_once_with(55555, expected=None)
        resume.assert_not_called()
        self.assertEqual(cg.read_snapshot()["reason"], "quota_unavailable")

    def test_weekly_exhaustion_pauses(self):
        suspend, _, _ = self.simulate(healthy(0, 100), [(55555, "S", "codex")])
        suspend.assert_called_once()

    def test_restart_recovers_only_matching_saved_pid(self):
        identity = {"start_time": "100", "boot_id": "boot"}
        cg.write_snapshot({"reason": "quota_exceeded", "active_pids": [55555], "process_identities": {"55555": identity}})
        with patch.object(cg.ProcessManager, "process_identity", return_value=identity):
            _, resume, _ = self.simulate(healthy(), [(55555, "T", "codex")])
        resume.assert_called_once_with(55555, expected=identity)

    def test_pid_reuse_does_not_resume_new_process(self):
        cg.write_snapshot({"reason": "quota_exceeded", "active_pids": [55555], "process_identities": {"55555": {"start_time": "old"}}})
        with patch.object(cg.ProcessManager, "process_identity", return_value={"start_time": "new"}):
            _, resume, launch = self.simulate(healthy(), [(55555, "T", "codex")])
        resume.assert_not_called()
        launch.assert_not_called()

    def test_manually_paused_or_stopped_is_never_auto_restarted(self):
        for reason in ["manual_pause", "manual_top_pause", "manual_stopped"]:
            cg.write_snapshot({"reason": reason, "sessions": [{"id": "old"}]})
            suspend, resume, launch = self.simulate(healthy(), [(55555, "T", "codex")])
            suspend.assert_not_called()
            resume.assert_not_called()
            launch.assert_not_called()
            self.assertEqual(cg.read_snapshot()["reason"], reason)

    def test_failed_launch_remains_pending(self):
        cg.write_snapshot({"reason": "quota_exceeded", "sessions": [{"id": "old"}]})
        with patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False):
            _, _, launch = self.simulate(healthy(), [])
        launch.assert_called_once_with("old", prompt="")
        self.assertEqual(cg.read_snapshot()["reason"], "quota_exceeded")

    def test_unrelated_client_does_not_prevent_saved_session_relaunch(self):
        cg.write_snapshot({"reason": "quota_exceeded", "sessions": [{"id": "saved"}]})
        with patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False):
            _, _, launch = self.simulate(healthy(), [(55555, "S", "codex unrelated")])
        launch.assert_called_once_with("saved", prompt="")

    def test_empty_snapshot_does_not_launch_history(self):
        cg.write_snapshot({"reason": "quota_exceeded"})
        with patch.object(cg.SessionManager, "get_recent_sessions") as history:
            _, _, launch = self.simulate(healthy(), [])
        history.assert_not_called()
        launch.assert_not_called()

    def test_interrupt_error_is_not_reported_as_success(self):
        with patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["thread"]), \
             patch.object(cg.CodexSocketClient, "query_app_server", side_effect=[
                 {"thread": {"turns": [{"id": "turn", "status": "inProgress"}]}}, None]):
            self.assertEqual(cg.SessionManager.interrupt_active_turns(), [])

    def test_restore_does_not_queue_unrelated_loaded_session(self):
        cg.write_snapshot({"sessions": [{"id": "saved"}]})
        with patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["saved", "unrelated"]), \
             patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False), \
             patch.object(cg.SessionManager, "get_thread_info", return_value={}), \
             patch.object(cg.SessionManager, "get_live_thread_state", return_value={"status_type": "idle"}), \
             patch.object(cg.SessionManager, "send_continue_prompt", return_value=True) as send, patch("time.sleep"):
            cg.SessionManager.restore_and_verify_sessions([])
        send.assert_called_once_with("saved", "继续")


class WebSocketTests(IsolatedTest):
    @staticmethod
    def frame(data, opcode=1, final=True):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        length = len(data)
        header = bytes([(0x80 if final else 0) | opcode])
        header += bytes([length]) if length < 126 else bytes([126]) + struct.pack("!H", length)
        return header + data

    @staticmethod
    def read_client(sock):
        def exact(n):
            data = b""
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    raise EOFError()
                data += chunk
            return data
        b1, b2 = exact(2)
        length = b2 & 127
        if length == 126:
            length = struct.unpack("!H", exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", exact(8))[0]
        mask = exact(4)
        data = exact(length)
        return b1 & 15, bytes(v ^ mask[i % 4] for i, v in enumerate(data))

    def query(self, mode="success"):
        path = str(Path(self.tmp.name) / "rpc.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(path)
        server.listen(1)
        errors = []
        observed = []
        def serve():
            try:
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(2)
                    header = b""
                    while b"\r\n\r\n" not in header:
                        header += conn.recv(1024)
                    key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key:"))
                    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    if mode == "bad_handshake":
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                        return
                    upgrade = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                    # Coalesce a complete initialize reply with HTTP headers: old client lost it.
                    init = {"id": 1, "result": {"userAgent": "test"}}
                    if mode == "init_error":
                        init = {"id": 1, "error": {"code": -1}}
                    conn.sendall(upgrade + self.frame(init))
                    opcode, payload = self.read_client(conn)
                    observed.append(json.loads(payload))
                    if mode == "init_error":
                        return
                    opcode, payload = self.read_client(conn)
                    observed.append(json.loads(payload))
                    opcode, payload = self.read_client(conn)
                    observed.append(json.loads(payload))
                    if mode == "rpc_error":
                        conn.sendall(self.frame({"id": 2, "error": {"code": -1}}))
                    elif mode == "oversized":
                        conn.sendall(b"\x81\x7f" + struct.pack("!Q", 32 * 1024 * 1024))
                    elif mode == "timeout":
                        time.sleep(0.25)
                    else:
                        payload = json.dumps({"id": 2, "result": {"ok": True}}).encode()
                        conn.sendall(self.frame(payload[:10], final=False) + self.frame(b"ping", opcode=9) + self.frame(payload[10:], opcode=0))
                        opcode, pong = self.read_client(conn)
                        observed.append({"pong": pong.decode(), "opcode": opcode})
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        with patch.object(cg.CodexSocketClient, "ensure_daemon_running", return_value=True), \
             patch.object(cg.CodexSocketClient, "get_socket_path", return_value=path):
            result = cg.CodexSocketClient.query_app_server("test/read", timeout=0.1 if mode == "timeout" else 1)
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        return result, observed

    def test_coalesced_handshake_fragmentation_ping_and_initialized(self):
        result, observed = self.query()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(observed[1], {"method": "initialized"})
        self.assertEqual(observed[2]["method"], "test/read")
        self.assertEqual(observed[3], {"pong": "ping", "opcode": 10})

    def test_http_rejection(self):
        self.assertIsNone(self.query("bad_handshake")[0])

    def test_initialize_error_prevents_method_dispatch(self):
        result, observed = self.query("init_error")
        self.assertIsNone(result)
        self.assertEqual(len(observed), 1)

    def test_rpc_error_is_not_an_empty_success(self):
        self.assertIsNone(self.query("rpc_error")[0])

    def test_oversized_frame_is_rejected_before_body(self):
        self.assertIsNone(self.query("oversized")[0])

    def test_deadline_is_bounded(self):
        start = time.monotonic()
        self.assertIsNone(self.query("timeout")[0])
        self.assertLess(time.monotonic() - start, 1)

    def test_socket_directory_must_be_owned_and_not_world_writable(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        path = str(Path(self.tmp.name) / "trusted.sock")
        sock.bind(path)
        self.assertTrue(cg.CodexSocketClient.is_trusted_socket(path))
        os.chmod(self.tmp.name, 0o777)
        self.assertFalse(cg.CodexSocketClient.is_trusted_socket(path))

    def test_socket_discovery_uses_custom_codex_home(self):
        directory = Path(self.tmp.name) / "app-server-control"
        directory.mkdir(mode=0o700)
        path = directory / "app-server-control.sock"
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(path))
        self.assertEqual(cg.CodexSocketClient.get_socket_path(), str(path))

    def test_loaded_thread_pagination_and_cycle(self):
        with patch.object(cg.CodexSocketClient, "query_app_server", side_effect=[
            {"data": ["one"], "nextCursor": "next"}, {"data": ["two", "one"], "nextCursor": None}]):
            self.assertEqual(cg.CodexSocketClient.query_loaded_threads(), ["one", "two"])
        with patch.object(cg.CodexSocketClient, "query_app_server", return_value={"data": [], "nextCursor": "loop"}):
            self.assertEqual(cg.CodexSocketClient.query_loaded_threads(), [])


class CliAndServiceTests(IsolatedTest):
    def test_public_start_and_stop_have_no_extra_modes(self):
        for name, handler in [("codexguard-start", "cmd_guard_start"), ("codexguard-stop", "cmd_guard_stop")]:
            with patch.object(sys, "argv", [name]), patch.object(cg, handler) as command:
                cg.main()
            command.assert_called_once_with()
            with patch.object(sys, "argv", [name, "--kill-codex"]), patch.object(cg, handler) as command:
                with self.assertRaises(SystemExit) as result:
                    cg.main()
            self.assertEqual(result.exception.code, 2)
            command.assert_not_called()

    def test_public_start_is_idempotent_systemd_start(self):
        with patch("subprocess.run") as run:
            cg.cmd_guard_start()
        run.assert_called_once_with(["systemctl", "--user", "start", cg.SERVICE_NAME], check=True, timeout=10)

    def test_public_stop_releases_only_recorded_paused_clients(self):
        identity = {"start_time": "100", "boot_id": "boot"}
        cg.write_snapshot({"reason": "quota_exceeded", "active_pids": [55555, 55556],
                           "process_identities": {"55555": identity}})
        with patch("subprocess.run") as run, \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[(55555, "T", "codex"), (55556, "T", "codex"), (55557, "T", "codex")]), \
             patch.object(cg.ProcessManager, "resume_pid", return_value=True) as resume, \
             patch.object(cg.ProcessManager, "terminate_pid") as terminate, \
             patch.object(cg.SessionManager, "send_continue_prompt") as queue:
            cg.cmd_guard_stop()
        run.assert_called_once_with(["systemctl", "--user", "stop", cg.SERVICE_NAME], check=True, timeout=10)
        resume.assert_called_once_with(55555, expected=identity)
        terminate.assert_not_called()
        queue.assert_not_called()
        self.assertEqual(cg.read_snapshot()["reason"], "manual_stopped")

    def test_kill_alias_and_subcommand_explicitly_request_client_termination(self):
        for argv in [["codex-kill"], ["codex-guard", "kill"]]:
            with patch.object(sys, "argv", list(argv)), patch.object(cg, "cmd_stop") as stop:
                cg.main()
            self.assertTrue(stop.call_args.args[0].kill_codex)
        with patch.object(sys, "argv", ["codex-stop"]), patch.object(cg, "cmd_stop") as stop:
            cg.main()
        self.assertFalse(stop.call_args.args[0].kill_codex)

    def test_run_waits_for_verified_quota_before_starting(self):
        args = MagicMock(threshold=3, interval=1)
        with patch("shutil.which", return_value="/tmp/codex"), \
             patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=None), \
             patch("subprocess.Popen") as launch, patch("time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cg.cmd_run(args, [])
        launch.assert_not_called()

    def test_run_only_controls_wrapped_child_and_forwards_arguments(self):
        args = MagicMock(threshold=3, interval=1)
        child = MagicMock(pid=55555, returncode=0)
        child.poll.side_effect = [None, None, 0]
        with patch("shutil.which", return_value="/tmp/codex"), \
             patch.object(cg.CodexSocketClient, "query_rate_limits", side_effect=[healthy(), None, healthy()]), \
             patch("subprocess.Popen", return_value=child) as launch, \
             patch.object(cg.ProcessManager, "suspend_pid", return_value=True) as suspend, \
             patch.object(cg.ProcessManager, "resume_pid", return_value=True) as resume, \
             patch.object(cg.SessionManager, "interrupt_active_turns") as interrupt, \
             patch.object(cg.SessionManager, "restore_and_verify_sessions") as restore, patch("time.sleep"):
            with self.assertRaises(SystemExit) as result:
                cg.cmd_run(args, ["--", "resume", "session", "-c", 'model="x"'])
        self.assertEqual(result.exception.code, 0)
        launch.assert_called_once_with(["/tmp/codex", "resume", "session", "-c", 'model="x"'])
        suspend.assert_called_once_with(55555)
        resume.assert_called_once_with(55555)
        interrupt.assert_not_called()
        restore.assert_not_called()

    def test_uncertain_terminal_launch_does_not_launch_multiple_fallbacks(self):
        with patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "get_thread_info", return_value={"cwd": self.tmp.name}), \
             patch.object(cg, "get_gui_env", return_value={}), \
             patch("shutil.which", return_value="/tmp/codex"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("terminal", 5)) as run, \
             patch("subprocess.Popen") as popen:
            self.assertFalse(cg.SessionManager.launch_codex_session("session"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["cwd"], self.tmp.name)
        popen.assert_not_called()

    def test_cli_rejects_invalid_ranges_and_unknown_flags(self):
        for argv in [["watch", "--threshold", "nan"], ["watch", "--threshold", "100"],
                     ["watch", "--interval", "0"], ["top", "--delay", "-1"],
                     ["watch", "--idle-delay", "-1"], ["watch", "--threshol", "3"],
                     ["service", "install", "--interval", "-2"]]:
            result = subprocess.run([sys.executable, str(ROOT / "bin/codex-guard")] + argv,
                                    capture_output=True, timeout=3,
                                    env=dict(os.environ, CODEX_HOME=self.tmp.name))
            self.assertEqual(result.returncode, 2, argv)

    def test_service_prompt_cannot_inject_directives_or_expand_environment(self):
        service = str(Path(self.tmp.name) / "guard.service")
        args = MagicMock(action="install", threshold=3, interval=10, idle_delay=5,
                         auto_continue=False, continue_prompt='"\nExecStart=/bin/false\n$HOME %h \\')
        with patch.object(cg, "SERVICE_FILE", service), patch("subprocess.run"):
            cg.cmd_service(args)
        content = Path(service).read_text()
        self.assertEqual(sum(line.startswith("ExecStart=") for line in content.splitlines()), 1)
        self.assertIn('\\nExecStart=/bin/false\\n$$HOME %%h', content)
        self.assertIn("--no-auto-continue", content)
        self.assertIn("Restart=on-failure", content)

    def test_batch_output_strips_controls_from_titles_and_tool_output(self):
        dash = cg.CodexTopDashboard()
        d = {"snapshot": None, "processes": [{"pid": 123, "state": "S", "state_desc": "RUNNING", "cpu_pct": 0, "mem_mb": 0, "cmd": "codex"}],
             "threads": [{"id": "t", "title": "bad\x1b]52;c;clipboard\x07", "turns_count": 1, "last_turn_status": "completed",
                          "turns": [{"status": "completed", "items": [{"type": "commandExecution", "command": "ls\x1b[31m", "status": "completed"}]}]}],
             "service_active": False, "service_pid": "0", "time_str": "now", "uptime": "up", "load": "0"}
        output = io.StringIO()
        dash.render_batch_from_data(d, output)
        self.assertNotIn("\x1b]52", output.getvalue())
        self.assertNotIn("ls\x1b[31m", output.getvalue())

    def test_install_uninstall_with_special_home_and_all_aliases(self):
        home = Path(self.tmp.name) / "home with & | % $ spaces"
        home.mkdir()
        tools_dir = Path(self.tmp.name) / "tools"
        tools_dir.mkdir()
        fake = tools_dir / "systemctl"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        env = dict(os.environ, HOME=str(home), PATH=str(tools_dir) + os.pathsep + os.environ["PATH"])
        old_bin = home / ".local/bin"
        old_bin.mkdir(parents=True)
        for alias in ["codex-guard", "codex-quota", "codex-stop", "codex-kill", "codex-fix-mouse"]:
            (old_bin / alias).write_text("old installation")
        subprocess.run(["bash", str(ROOT / "scripts/install.sh")], env=env, capture_output=True, check=True)
        for alias in ["codexguard-start", "codexguard-stop", "codex-top"]:
            self.assertTrue((home / ".local/bin" / alias).exists(), alias)
        self.assertEqual({p.name for p in old_bin.iterdir()}, {"codexguard-start", "codexguard-stop", "codex-top"})
        self.assertTrue((home / ".local/lib/codex-guard/codex-guard").exists())
        unit = home / ".config/systemd/user/codex-quota-guard.service"
        self.assertIn('ExecStart="%h/', unit.read_text())
        subprocess.run(["bash", str(ROOT / "scripts/uninstall.sh")], env=env, capture_output=True, check=True)
        self.assertFalse(unit.exists())
        self.assertEqual(list((home / ".local/bin").iterdir()), [])
        self.assertFalse((home / ".local/lib/codex-guard").exists())


class DashboardWheelTests(IsolatedTest):
    def test_escape_mouse_and_arrow_sequences_do_not_become_shortcuts(self):
        sequences = [b"\x1b[<65;20;10M", b"\x1b[<64;20;10M",
                     b"\x1b[Maqr", b"\x1b[Maga", b"\x1b[M`+-", b"\x1b[B", b"\x1bOA"]
        for sequence in sequences:
            key_filter = cg.TerminalKeyFilter()
            output = [key_filter.feed(byte) for byte in sequence]
            self.assertTrue(all(key == -1 for key in output), sequence)
            self.assertEqual(key_filter.feed(ord('q')), ord('q'))

    def test_curses_wheel_does_not_exit_before_q(self):
        if cg.curses is None:
            self.skipTest("curses unavailable")
        keys = list(b"\x1b[<65;20;10M\x1b[Maqr") + [ord('q')]
        screen = MagicMock()
        screen.getch.side_effect = keys
        screen.getmaxyx.return_value = (24, 100)
        dash = cg.CodexTopDashboard()
        with patch("threading.Thread"), patch.object(cg.sys, "stdout", io.StringIO()), \
             patch.object(dash, "get_snapshot", return_value={}), \
             patch.object(dash, "draw_curses_screen"):
            dash.run_curses(screen)
        self.assertEqual(screen.getch.call_count, len(keys))

    def test_ansi_wheel_survives_in_real_pty_and_q_still_exits(self):
        import pty
        import select
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        script = f'''
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("guard_pty", {str(ROOT / "bin/codex-guard")!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)
dash = cg.CodexTopDashboard()
dash._background_collector = lambda: dash._stop_event.wait()
dash.get_snapshot = lambda: {{}}
dash.render_batch_from_data = lambda data, out: (out.write("READY\\n"), out.flush())
dash.run_ansi_loop()
'''
        proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave)
        self.addCleanup(lambda: proc.poll() is None and proc.wait(timeout=3))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        received = b""
        deadline = time.monotonic() + 3
        while b"READY" not in received and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                received += os.read(master, 4096)
        self.assertIn(b"READY", received)
        os.write(master, b"\x1b[<65;20;10M\x1b[Maqr")
        time.sleep(0.15)
        self.assertIsNone(proc.poll(), "Wheel report exited the dashboard")
        os.write(master, b"q")
        self.assertEqual(proc.wait(timeout=3), 0)


class DashboardControlTests(IsolatedTest):
    def test_auto_continue_setting_is_private_persistent_and_toggleable(self):
        dashboard = cg.CodexTopDashboard()
        status = {"service_active": True, "service_state": "active", "service_pid": "42", "default_auto_continue": True}
        with patch.object(cg, "read_guard_service", return_value=status):
            dashboard.toggle_auto_continue()
            self.assertFalse(cg.get_auto_continue())
            self.assertFalse(cg.get_auto_continue(default=True))
            self.assertEqual(os.stat(Path(self.tmp.name) / "quota_guard_settings.json").st_mode & 0o777, 0o600)
            dashboard.toggle_auto_continue()
            self.assertTrue(cg.get_auto_continue(default=False))
        Path(self.tmp.name, "quota_guard_settings.json").write_text('{"auto_continue": "false"}')
        self.assertFalse(cg.get_auto_continue())

    def test_guard_toggle_uses_fresh_service_state(self):
        dashboard = cg.CodexTopDashboard()
        with patch.object(cg, "read_guard_service", side_effect=[
            {"service_active": False, "service_state": "inactive"},
            {"service_active": True, "service_state": "active"}]), \
             patch.object(cg, "cmd_guard_start") as start, patch.object(cg, "cmd_guard_stop") as stop:
            dashboard.toggle_guard()
            dashboard.toggle_guard()
        start.assert_called_once_with(quiet=True)
        stop.assert_called_once_with(quiet=True)

    def test_failed_stop_does_not_disable_a_running_guard_through_snapshot(self):
        cg.write_snapshot({"reason": "quota_exceeded"})
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "systemctl")), \
             patch.object(cg.ProcessManager, "resume_pid") as resume:
            with self.assertRaises(subprocess.CalledProcessError):
                cg.cmd_guard_stop(quiet=True)
        self.assertEqual(cg.read_snapshot()["reason"], "quota_exceeded")
        resume.assert_not_called()

    def test_control_actions_report_failure_without_faking_state(self):
        dashboard = cg.CodexTopDashboard()
        with patch.object(dashboard, "toggle_guard", side_effect=RuntimeError("service unavailable")):
            self.assertTrue(dashboard.handle_control_key(ord('g')))
            dashboard._action_thread.join(timeout=2)
        self.assertIn("操作失败", dashboard.flash_message)
        self.assertIn("service unavailable", dashboard.flash_message)
        self.assertFalse(dashboard._action_lock.locked())

    def test_controls_do_not_block_rendering_or_overlap(self):
        dashboard = cg.CodexTopDashboard()
        entered, finish = threading.Event(), threading.Event()
        def action():
            entered.set()
            finish.wait(timeout=2)
            return "done"
        with patch.object(dashboard, "toggle_guard", side_effect=action) as toggle:
            self.assertTrue(dashboard.handle_control_key(ord('g')))
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(dashboard.handle_control_key(ord('g')))
            self.assertIn("请稍候", dashboard.flash_message)
            finish.set()
            dashboard._action_thread.join(timeout=2)
        self.assertEqual(toggle.call_count, 1)

    def test_backend_reads_auto_continue_changes_without_restart(self):
        cg.set_auto_continue(False)
        sleeps = []
        def sleep(duration):
            sleeps.append(duration)
            if len(sleeps) == 1:
                cg.set_auto_continue(True)
            else:
                raise KeyboardInterrupt()
        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=healthy()), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[(55555, "S", "codex")]), \
             patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["thread"]), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False), \
             patch.object(cg.SessionManager, "get_live_thread_state", return_value={
                 "status_type": "idle", "last_turn_id": "turn", "last_turn_status": "completed"}), \
             patch.object(cg.SessionManager, "send_continue_prompt", return_value=True) as queue, \
             patch.object(cg, "send_notification"), patch("time.sleep", side_effect=sleep):
            cg.run_watch_loop(interval=1, idle_delay=0)
        queue.assert_called_once_with("thread", "继续")
        self.assertTrue(cg.read_guard_json("quota_guard_runtime.json")["auto_continue"])

    def test_recovery_does_not_send_prompts_when_auto_continue_is_off(self):
        cg.set_auto_continue(False)
        cg.write_snapshot({"reason": "quota_exceeded", "sessions": [{"id": "recorded"}]})
        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=healthy()), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[]), \
             patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False), \
             patch.object(cg.SessionManager, "launch_codex_session", return_value=False) as launch, \
             patch.object(cg.SessionManager, "restore_and_verify_sessions", return_value={"sessions": []}) as restore, \
             patch.object(cg, "send_notification"), patch("time.sleep", side_effect=KeyboardInterrupt):
            cg.run_watch_loop(interval=1)
        launch.assert_called_once_with("recorded", prompt="")
        self.assertFalse(restore.call_args.kwargs["send_continue"])

    def test_auto_continue_off_during_recovery_prevents_messages(self):
        cg.set_auto_continue(True)
        cg.write_snapshot({"sessions": [{"id": "thread"}]})
        with patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["thread"]), \
             patch.object(cg.SessionManager, "get_root_thread_id", side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, "is_subagent", return_value=False), \
             patch.object(cg.SessionManager, "get_thread_info", return_value={}), \
             patch.object(cg.SessionManager, "get_live_thread_state", return_value={"status_type": "idle"}), \
             patch.object(cg.SessionManager, "send_continue_prompt") as send, \
             patch("time.sleep", side_effect=lambda _: cg.set_auto_continue(False)):
            cg.SessionManager.restore_and_verify_sessions([], automatic=True)
        send.assert_not_called()

    def test_runtime_confirmation_and_stopped_guard_are_visible(self):
        self.assertIn("待后台应用", cg.CodexTopDashboard.control_status({
            "service_active": True, "auto_continue_enabled": False, "auto_continue_pending": True}))
        self.assertIn("守护关闭，未执行", cg.CodexTopDashboard.control_status({
            "service_active": False, "auto_continue_enabled": True}))


if __name__ == "__main__":
    unittest.main()
