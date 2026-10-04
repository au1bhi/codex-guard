#!/usr/bin/env python3
"""
Comprehensive Unit & Integration Test Suite for Codex Quota Guard & Codex Top
"""

import os
import sys
import time
import json
import sqlite3
import tempfile
import unittest
import importlib.machinery
import importlib.util
from unittest.mock import patch, MagicMock

# Dynamically import codex-guard module from /tmp/codex-guard/bin/codex-guard
loader = importlib.machinery.SourceFileLoader("codex_guard", "/tmp/codex-guard/bin/codex-guard")
spec = importlib.util.spec_from_loader(loader.name, loader)
cg = importlib.util.module_from_spec(spec)
loader.exec_module(cg)


class TestQuotaSnapshot(unittest.TestCase):
    def test_healthy_quota(self):
        raw = {
            "rateLimits": {
                "planType": "plus",
                "credits": {"hasCredits": True, "balance": "1311.14"},
                "primary": {"windowDurationMins": 300, "usedPercent": 4.0, "resetsAt": int(time.time() + 18000)},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 50.0}
            }
        }
        snap = cg.QuotaSnapshot(raw)
        self.assertEqual(snap.plan_type, "plus")
        self.assertTrue(snap.has_credits)
        self.assertEqual(snap.credit_balance, "1311.14")
        self.assertAlmostEqual(snap.used_percent, 4.0)
        self.assertAlmostEqual(snap.remaining_percent, 96.0)
        self.assertGreater(snap.seconds_until_reset, 17000)
        self.assertIn("小时", snap.time_to_reset_str)

    def test_low_quota(self):
        raw = {
            "rateLimits": {
                "planType": "plus",
                "credits": {"hasCredits": True, "balance": "1311.14"},
                "primary": {"windowDurationMins": 300, "usedPercent": 98.5, "resetsAt": int(time.time() + 120)},
            }
        }
        snap = cg.QuotaSnapshot(raw)
        self.assertAlmostEqual(snap.used_percent, 98.5)
        self.assertAlmostEqual(snap.remaining_percent, 1.5)
        self.assertIn("分钟", snap.time_to_reset_str)

    def test_expired_reset(self):
        raw = {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 100.0, "resetsAt": int(time.time() - 10)},
            }
        }
        snap = cg.QuotaSnapshot(raw)
        self.assertEqual(snap.seconds_until_reset, 0.0)
        self.assertEqual(snap.time_to_reset_str, "已到期/即将刷新")


class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state_test.sqlite")
        self.snap_file = os.path.join(self.temp_dir.name, "snapshot_test.json")

        # Create mock SQLite database
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                cwd TEXT,
                updated_at INTEGER,
                source TEXT,
                model TEXT,
                archived INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            INSERT INTO threads VALUES 
            ('thread-001', 'Task 1: Optimize Kernels', '/tmp/proj', 1700000000, 'cli', 'gpt-5-codex', 0),
            ('thread-002', 'Task 2: Debug Crash', '/home/user', 1700000500, 'cli', 'gpt-5-codex', 0),
            ('thread-003', 'Archived Task', '/tmp', 1600000000, 'cli', 'gpt-5-codex', 1),
            ('thread-sub-01', 'Subagent Worker', '/tmp/proj', 1700000600, '{"subagent": {"thread_spawn": {"parent_thread_id": "thread-001"}}}', 'gpt-5-codex', 0)
        """)
        conn.commit()
        conn.close()

        self.orig_db = cg.SessionManager.DB_PATH
        self.orig_snap = cg.SNAPSHOT_FILE
        cg.SessionManager.DB_PATH = self.db_path
        cg.SNAPSHOT_FILE = self.snap_file

    def tearDown(self):
        cg.SessionManager.DB_PATH = self.orig_db
        cg.SNAPSHOT_FILE = self.orig_snap
        self.temp_dir.cleanup()

    def test_get_thread_info_readonly(self):
        info = cg.SessionManager.get_thread_info("thread-001")
        self.assertIsNotNone(info)
        self.assertEqual(info["id"], "thread-001")
        self.assertEqual(info["title"], "Task 1: Optimize Kernels")
        self.assertEqual(info["cwd"], "/tmp/proj")

    def test_get_recent_sessions(self):
        sessions = cg.SessionManager.get_recent_sessions(limit=10)
        self.assertEqual(len(sessions), 2)  # thread-003 is archived, so excluded
        self.assertEqual(sessions[0]["id"], "thread-002")  # most recent first
        self.assertEqual(sessions[1]["id"], "thread-001")

    def test_save_snapshot_and_load(self):
        with patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["thread-001"]):
            snapshot = cg.SessionManager.save_snapshot(
                active_pids=[12345],
                reason="quota_low",
                interrupted_turns=[{"threadId": "thread-001", "turnId": "turn-99", "title": "Task 1"}]
            )
            self.assertEqual(snapshot["reason"], "quota_low")
            self.assertEqual(snapshot["active_pids"], [12345])
            self.assertEqual(len(snapshot["sessions"]), 1)
            self.assertEqual(snapshot["sessions"][0]["id"], "thread-001")
            self.assertTrue(os.path.exists(self.snap_file))

    def test_launch_codex_session_fallback(self):
        with patch("subprocess.run") as mock_run, patch("shutil.which", return_value="/usr/local/bin/codex"):
            # Mock gnome-terminal succeeds
            mock_run.return_value = MagicMock(returncode=0)
            res = cg.SessionManager.launch_codex_session("sess-1234", prompt="继续")
            self.assertTrue(res)
            mock_run.assert_called()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "gnome-terminal")
            self.assertIn("resume", args[5])
            self.assertIn("sess-1234", args[5])
            self.assertIn("继续", args[5])

    def test_restore_and_verify_sessions_exclude(self):
        with patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["thread-001", "thread-002"]), \
             patch.object(cg.SessionManager, "send_continue_prompt", return_value=True) as mock_send, \
             patch("time.sleep"):
            # Exclude thread-001 because it was newly launched in separate terminal
            res = cg.SessionManager.restore_and_verify_sessions(
                resumed_pids=[],
                send_continue=True,
                continue_prompt="继续",
                exclude_thread_ids=["thread-001"]
            )
            # Only thread-002 should receive queue send_continue_prompt
            mock_send.assert_called_once_with("thread-002", "继续")
            self.assertEqual(len(res["sessions"]), 2)

    def test_subagent_detection_and_resolution(self):
        # thread-001 is root
        self.assertFalse(cg.SessionManager.is_subagent("thread-001"))
        self.assertEqual(cg.SessionManager.get_root_thread_id("thread-001"), "thread-001")

        # thread-sub-01 is a subagent whose parent is thread-001
        self.assertTrue(cg.SessionManager.is_subagent("thread-sub-01"))
        self.assertEqual(cg.SessionManager.get_root_thread_id("thread-sub-01"), "thread-001")

    def test_send_continue_prompt_skips_subagents(self):
        with patch("subprocess.run") as mock_run:
            # Must skip subagents without executing subprocess
            res = cg.SessionManager.send_continue_prompt("thread-sub-01", "继续")
            self.assertFalse(res)
            mock_run.assert_not_called()

    def test_launch_codex_session_resolves_root_thread(self):
        with patch("subprocess.run") as mock_run, patch("shutil.which", return_value="/usr/local/bin/codex"):
            mock_run.return_value = MagicMock(returncode=0)
            # Launching with subagent ID should resolve to parent thread-001
            res = cg.SessionManager.launch_codex_session("thread-sub-01", prompt="继续")
            self.assertTrue(res)
            mock_run.assert_called()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "gnome-terminal")
            self.assertIn("thread-001", args[5])
            self.assertNotIn("thread-sub-01", args[5])

    def test_get_gui_env(self):
        env = cg.get_gui_env()
        self.assertIn("DISPLAY", env)
        self.assertIn("XDG_RUNTIME_DIR", env)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", env)


class TestProcessManager(unittest.TestCase):
    def test_process_exclusion_rules(self):
        # Verify that internal daemons and codex-top are never targeted
        excluded_cmds = [
            "python3 /home/user/.local/bin/codex-top",
            "codex-top",
            "codex app-server --listen unix:// --managed-daemon",
            "/bin/codex app-server daemon pid-update-loop",
            "codex-code-mode-host",
            "python3 /home/user/.local/bin/codex-guard watch",
        ]
        for cmd in excluded_cmds:
            self.assertTrue(
                any(x in cmd for x in ["app-server", "daemon", "code-mode-host", "quota-guard", "codex-guard", "codex-top", "codex-quota"]),
                f"Command '{cmd}' should be excluded from suspend targets"
            )

        # Real codex client command should NOT be excluded
        client_cmd = "codex resume 01a0f0c8-5afb-7b13-9986-23fecae1b2bd 继续"
        self.assertFalse(
            any(x in client_cmd for x in ["app-server", "daemon", "code-mode-host", "quota-guard", "codex-guard", "codex-top", "codex-quota"])
        )


class TestCodexTopDashboard(unittest.TestCase):
    def setUp(self):
        self.dash = cg.CodexTopDashboard(delay=1.0)

    def test_cpu_tracker_memory_leak_pruning(self):
        # Simulate old PIDs in cpu_tracker
        self.dash.cpu_tracker[99991] = (100, time.time() - 2)
        self.dash.cpu_tracker[99992] = (200, time.time() - 2)

        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=None), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[(99991, "S", "codex")]), \
             patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=[]), \
             patch.object(self.dash, "get_proc_metrics", return_value={"pid": 99991, "state": "S", "state_desc": "RUNNING", "cpu_pct": 1.0, "mem_mb": 50.0}):
            
            data = self.dash.collect_data()
            # PID 99992 should have been pruned from cpu_tracker
            self.assertIn(99991, self.dash.cpu_tracker)
            self.assertNotIn(99992, self.dash.cpu_tracker)

    def test_curses_screen_render_no_crash(self):
        # Test extreme terminal size handling
        mock_stdscr = MagicMock()
        mock_data = {
            "snapshot": None,
            "processes": [],
            "threads": [],
            "uptime": "up 1h",
            "load": "0.10, 0.20, 0.30",
            "service_active": True,
            "service_pid": "1234",
            "time_str": "12:00:00"
        }
        # Very small terminal: 30 cols x 5 rows
        self.dash.draw_curses_screen(mock_stdscr, mock_data, 5, 30)
        mock_stdscr.addnstr.assert_called()

        # Large terminal: 140 cols x 40 rows
        mock_stdscr.reset_mock()
        self.dash.draw_curses_screen(mock_stdscr, mock_data, 40, 140)
        mock_stdscr.addnstr.assert_called()


class TestWatchLoopSimulation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.snap_file = os.path.join(self.temp_dir.name, "snapshot.json")
        self.orig_snap = cg.SNAPSHOT_FILE
        cg.SNAPSHOT_FILE = self.snap_file

    def tearDown(self):
        cg.SNAPSHOT_FILE = self.orig_snap
        self.temp_dir.cleanup()

    def test_interrupt_and_suspend_on_quota_drop(self):
        """Simulate quota dropping to 1.5% -> verify interrupt and suspend"""
        raw_low = {
            "rateLimits": {
                "planType": "plus",
                "credits": {"hasCredits": True, "balance": "1311.14"},
                "primary": {"windowDurationMins": 300, "usedPercent": 98.5, "resetsAt": int(time.time() + 60)}
            }
        }
        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=raw_low), \
             patch.object(cg.SessionManager, "interrupt_active_turns", return_value=[{"threadId": "t1", "turnId": "turn-1", "title": "Task 1"}]) as mock_interrupt, \
             patch.object(cg.SessionManager, "kill_orphaned_tool_subprocesses") as mock_kill_tools, \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[(5555, "S", "codex")]), \
             patch.object(cg.ProcessManager, "suspend_pid", return_value=True) as mock_suspend, \
             patch.object(cg, "send_notification") as mock_notify, \
             patch("time.sleep", side_effect=KeyboardInterrupt):  # Stop loop after 1 iteration

            try:
                cg.run_watch_loop(threshold=3.0, interval=1)
            except KeyboardInterrupt:
                pass

            mock_interrupt.assert_called_once()
            mock_kill_tools.assert_called_once()
            mock_suspend.assert_called_once_with(5555)
            self.assertTrue(os.path.exists(self.snap_file))
            with open(self.snap_file) as f:
                snap = json.load(f)
                self.assertEqual(snap["reason"], "quota_exceeded")
                self.assertIn(5555, snap["active_pids"])

    def test_auto_recovery_and_launch_when_quota_restored(self):
        """Simulate quota recovering to 95% after process was terminated -> verify auto-launch"""
        # Prepare snapshot of previously killed process
        snapshot_content = {
            "reason": "quota_exceeded",
            "sessions": [{"id": "t1", "title": "Kernel Opt"}],
            "interrupted_turns": [{"threadId": "t1", "turnId": "turn-1"}],
            "loaded_thread_ids": ["t1"]
        }
        with open(self.snap_file, "w") as f:
            json.dump(snapshot_content, f)

        raw_recovered = {
            "rateLimits": {
                "planType": "plus",
                "credits": {"hasCredits": True, "balance": "1311.14"},
                "primary": {"windowDurationMins": 300, "usedPercent": 5.0, "resetsAt": int(time.time() + 18000)}
            }
        }
        def fake_sleep(duration):
            if duration == 1:
                raise KeyboardInterrupt()

        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=raw_recovered), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[]), \
             patch.object(cg.SessionManager, "launch_codex_session", return_value=True) as mock_launch, \
             patch.object(cg.SessionManager, "restore_and_verify_sessions") as mock_restore, \
             patch.object(cg, "send_notification"), \
             patch("time.sleep", side_effect=fake_sleep):

            try:
                cg.run_watch_loop(threshold=3.0, interval=1)
            except KeyboardInterrupt:
                pass

            # Since no running clients existed, auto-launch should have been triggered for t1!
            mock_launch.assert_called_once_with("t1", prompt="继续")
            mock_restore.assert_called_once()
            # Verify exclude_thread_ids has t1 to prevent double injection
            exclude_arg = mock_restore.call_args[1].get("exclude_thread_ids")
            self.assertEqual(exclude_arg, ["t1"])

            # Verify snapshot reason updated to quota_recovered
            with open(self.snap_file) as f:
                snap = json.load(f)
                self.assertEqual(snap["reason"], "quota_recovered")

    def test_auto_recovery_launches_when_resumed_process_exited_immediately(self):
        """Regression test: verify auto-launch triggers even if a suspended PID existed when running_clients is empty, and resolves subagent to root"""
        snapshot_content = {
            "reason": "quota_exceeded",
            "sessions": [{"id": "thread-sub-01", "title": "Sub Task"}],
            "interrupted_turns": [{"threadId": "thread-sub-01", "turnId": "turn-9"}],
            "loaded_thread_ids": ["thread-sub-01"]
        }
        with open(self.snap_file, "w") as f:
            json.dump(snapshot_content, f)

        raw_recovered = {
            "rateLimits": {
                "planType": "plus",
                "credits": {"hasCredits": True, "balance": "1311.14"},
                "primary": {"windowDurationMins": 300, "usedPercent": 5.0, "resetsAt": int(time.time() + 18000)}
            }
        }
        def fake_sleep(duration):
            if duration == 1:
                raise KeyboardInterrupt()

        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=raw_recovered), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[]), \
             patch.object(cg.SessionManager, "get_thread_info", side_effect=lambda tid: {"source": '{"subagent": {"thread_spawn": {"parent_thread_id": "thread-001"}}}'} if tid == "thread-sub-01" else {"source": "cli"}), \
             patch.object(cg.SessionManager, "launch_codex_session", return_value=True) as mock_launch, \
             patch.object(cg.SessionManager, "restore_and_verify_sessions") as mock_restore, \
             patch.object(cg, "send_notification"), \
             patch("time.sleep", side_effect=fake_sleep):

            try:
                cg.run_watch_loop(threshold=3.0, interval=1)
            except KeyboardInterrupt:
                pass

            # Auto-launch MUST be called, and it MUST resolve thread-sub-01 to root thread-001!
            mock_launch.assert_called_once_with("thread-001", prompt="继续")
            mock_restore.assert_called_once()

    def test_auto_continue_on_idle(self):
        """Simulate idle session -> verify debouncing and send_continue_prompt"""
        raw_healthy = {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 5.0, "resetsAt": int(time.time() + 18000)}
            }
        }
        live_idle = {
            "status_type": "idle",
            "last_turn_id": "turn-abc",
            "last_turn_status": "completed"
        }

        call_count = [0]
        def fake_sleep(duration):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt()

        with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=raw_healthy), \
             patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=[(1001, "S", "codex")]), \
             patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=["tid-idle"]), \
             patch.object(cg.SessionManager, "get_live_thread_state", return_value=live_idle), \
             patch.object(cg.SessionManager, "send_continue_prompt", return_value=True) as mock_send, \
             patch.object(cg, "send_notification"), \
             patch("time.sleep", side_effect=fake_sleep):

            try:
                # idle_delay = 0 so it triggers immediately
                cg.run_watch_loop(threshold=3.0, interval=1, idle_delay=0)
            except KeyboardInterrupt:
                pass

            mock_send.assert_called_with("tid-idle", "继续")


class TestToolProcessTermination(unittest.TestCase):
    def test_recursive_descendant_killing(self):
        """Test kill_orphaned_tool_subprocesses discovers child and grandchild processes"""
        def fake_open(path, mode="r", *args, **kwargs):
            m = unittest.mock.mock_open()
            if "99990" in path:
                m.return_value.read.return_value = b"codex-code-mode-host"
            else:
                m.return_value.read.return_value = b"python3 train_kernel.py"
            return m()

        with patch("glob.glob", return_value=["/proc/99990"]), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=fake_open), \
             patch("subprocess.run") as mock_run, \
             patch("os.kill") as mock_kill:

            def fake_pgrep(cmd, **kwargs):
                parent = cmd[2]
                res = MagicMock()
                if parent == "99990":
                    res.stdout = "99991\n"
                elif parent == "99991":
                    res.stdout = "99992\n"
                else:
                    res.stdout = ""
                return res

            mock_run.side_effect = fake_pgrep
            cg.SessionManager.kill_orphaned_tool_subprocesses()

            # Verify that leaf 99992 and intermediate 99991 were killed with SIGTERM
            killed_pids = [call[0][0] for call in mock_kill.call_args_list]
            self.assertIn(99992, killed_pids)
            self.assertIn(99991, killed_pids)


class TestPerformanceAndStress(unittest.TestCase):
    def test_rapid_resize_rendering_latency(self):
        """Stress test: 100 rapid terminal resizes must each render in under 5 milliseconds"""
        dash = cg.CodexTopDashboard()
        mock_stdscr = MagicMock()
        mock_data = {
            "snapshot": cg.QuotaSnapshot({
                "rateLimits": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 10.0, "resetsAt": int(time.time() + 10000)}
                }
            }),
            "processes": [{"pid": 12345, "state": "S", "state_desc": "RUNNING", "cpu_pct": 5.2, "mem_mb": 120.0, "cmd": "codex"}],
            "threads": [{"id": "t1", "title": "Rapid Test", "turns_count": 5, "last_turn_status": "inProgress", "status_type": "active", "turns": []}],
            "uptime": "up 10h",
            "load": "1.0, 1.0, 1.0",
            "service_active": True,
            "service_pid": "1000",
            "time_str": "12:34:56"
        }

        durations = []
        for i in range(100):
            # Vary terminal dimensions from 40x10 to 200x60
            cols = 40 + (i % 160)
            rows = 10 + (i % 50)
            t0 = time.perf_counter()
            dash.draw_curses_screen(mock_stdscr, mock_data, rows, cols)
            t1 = time.perf_counter()
            durations.append((t1 - t0) * 1000)

        max_ms = max(durations)
        avg_ms = sum(durations) / len(durations)
        # Assert each render takes well under 5ms (typically < 0.2ms)
        self.assertLess(max_ms, 5.0, f"Max render latency {max_ms:.2f}ms exceeded 5ms limit")
        self.assertLess(avg_ms, 1.0, f"Average render latency {avg_ms:.2f}ms exceeded 1ms limit")

    def test_cpu_tracker_stability_under_pid_churn(self):
        """Stress test: 1000 cycles of PIDs appearing and disappearing -> memory must stay bounded"""
        dash = cg.CodexTopDashboard()
        for cycle in range(500):
            # Spawn 10 ephemeral PIDs
            client_pids = [(cycle * 10 + i, "S", "codex") for i in range(10)]
            with patch.object(cg.CodexSocketClient, "query_rate_limits", return_value=None), \
                 patch.object(cg.ProcessManager, "get_codex_client_pids", return_value=client_pids), \
                 patch.object(cg.SessionManager, "get_loaded_thread_ids", return_value=[]), \
                 patch.object(dash, "get_proc_metrics", return_value={"pid": 1, "state": "S", "state_desc": "RUNNING", "cpu_pct": 0.0, "mem_mb": 0.0}):
                dash.collect_data()

        # At the end, only the last 10 PIDs should be tracked in cpu_tracker
        self.assertLessEqual(len(dash.cpu_tracker), 10, f"cpu_tracker has {len(dash.cpu_tracker)} entries, expected <= 10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
