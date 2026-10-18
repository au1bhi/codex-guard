"""Automatic pause ownership regressions; no host clients or RPCs are used."""
import unittest
from unittest.mock import patch

from test_security_regressions import IsolatedTest, cg, healthy


class AutomaticPauseResultsTests(IsolatedTest):
    def run_guard(self, quotas, clients, outcomes, sleeps=None, previous=None, identity_provider=None):
        if previous is not None:
            cg.write_snapshot(previous)
        with patch.object(cg.CodexSocketClient, 'query_rate_limits', side_effect=quotas), \
             patch.object(cg.ProcessManager, 'get_codex_client_pids', side_effect=clients), \
             patch.object(cg.ProcessManager, 'process_identity', side_effect=identity_provider or (lambda pid: {'pid': pid, 'start_time': 1})), \
             patch.object(cg.ProcessManager, 'suspend_pid', side_effect=outcomes) as suspend, \
             patch.object(cg.ProcessManager, 'resume_pid', return_value=True) as resume, \
             patch.object(cg.SessionManager, 'interrupt_active_turns', return_value=[]), \
             patch.object(cg.SessionManager, 'get_loaded_thread_ids', return_value=[]), \
             patch.object(cg.SessionManager, 'restore_and_verify_sessions', return_value={'sessions': []}), \
             patch.object(cg.SessionManager, 'launch_codex_session') as launch, \
             patch.object(cg, 'send_notification') as notify, \
             patch('time.sleep', side_effect=sleeps or [KeyboardInterrupt]):
            cg.run_watch_loop(auto_continue=False, interval=1)
        return cg.read_snapshot(), suspend, resume, notify, launch

    def test_partial_failure_keeps_only_successful_pause(self):
        saved, suspend, _, notify, _ = self.run_guard(
            [healthy(99)], [[(55555, 'S', 'codex'), (55556, 'S', 'codex')]], [True, False])
        self.assertEqual(suspend.call_count, 2)
        self.assertEqual(saved['active_pids'], [55555])
        self.assertEqual(set(saved['process_identities']), {'55555'})
        self.assertTrue(any(c.args[0] == 'Codex 暂停未完成' for c in notify.call_args_list))
        self.assertFalse(any(c.args[0] == 'Codex 已暂停' for c in notify.call_args_list))

    def test_failed_pause_is_retried_at_next_low_quota_poll(self):
        saved, suspend, _, _, _ = self.run_guard(
            [healthy(99), healthy(99)], [[(55555, 'S', 'codex')]] * 2,
            [False, True], [None, KeyboardInterrupt])
        self.assertEqual(suspend.call_count, 2)
        self.assertEqual(saved['active_pids'], [55555])
        self.assertEqual(saved['reason'], 'quota_exceeded')

    def test_failed_pause_does_not_own_later_user_pause_on_recovery(self):
        saved, _, resume, _, launch = self.run_guard(
            [healthy(99), healthy()],
            [[(55555, 'S', 'codex')], [(55555, 'T', 'codex')], [(55555, 'T', 'codex')]],
            [False], [None, KeyboardInterrupt])
        resume.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['reason'], 'quota_recovered')

    def test_newer_manual_stop_wins_over_pause_outcome_write(self):
        def failed_signal(pid, expected=None):
            cg.write_snapshot({'reason': 'manual_stopped', 'active_pids': []})
            return False
        saved, _, _, notify, _ = self.run_guard(
            [healthy(99)], [[(55555, 'S', 'codex')]], failed_signal)
        self.assertEqual(saved['reason'], 'manual_stopped')
        self.assertFalse(any(c.args[0] in {'Codex 已暂停', 'Codex 暂停未完成'} for c in notify.call_args_list))

    def test_failed_new_pause_preserves_earlier_owned_pause(self):
        previous = {'reason': 'quota_exceeded', 'active_pids': [55555],
                    'process_identities': {'55555': {'pid': 55555, 'start_time': 1}}}
        saved, _, _, _, _ = self.run_guard(
            [healthy(99)], [[(55555, 'T', 'codex'), (55556, 'S', 'codex')]], [False], previous=previous)
        self.assertEqual(saved['active_pids'], [55555])
        self.assertEqual(set(saved['process_identities']), {'55555'})

    def test_unavailable_quota_also_retires_failed_pause(self):
        saved, _, _, notify, _ = self.run_guard(
            [None], [[(55555, 'S', 'codex')]], [False])
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['reason'], 'quota_unavailable')
        self.assertTrue(any(c.args[0] == 'Codex 暂停未完成' for c in notify.call_args_list))


    def test_missing_recorded_identity_prevents_suspension(self):
        saved, suspend, _, notify, _ = self.run_guard(
            [healthy(99)], [[(55555, 'S', 'codex')]], [True],
            identity_provider=lambda pid: None)
        suspend.assert_not_called()
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['process_identities'], {})
        self.assertTrue(any(c.args[0] == 'Codex 暂停未完成' for c in notify.call_args_list))

    def test_missing_identity_is_retried_when_next_snapshot_can_record_it(self):
        identity = {'pid': 55555, 'start_time': 1}
        saved, suspend, _, _, _ = self.run_guard(
            [healthy(99), healthy(99)], [[(55555, 'S', 'codex')]] * 2,
            [True, True], [None, KeyboardInterrupt], identity_provider=[None, identity])
        suspend.assert_called_once_with(55555, expected=identity)
        self.assertEqual(saved['active_pids'], [55555])
        self.assertEqual(saved['process_identities'], {'55555': identity})


    def test_real_child_is_not_paused_when_snapshot_identity_read_fails(self):
        import os
        import signal
        import subprocess
        import time
        from pathlib import Path
        child = subprocess.Popen(['bash', '-c', 'exec -a codex sleep 30'])
        def cleanup():
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.kill()
            child.wait(timeout=3)
        self.addCleanup(cleanup)
        for _ in range(100):
            parts = Path('/proc/%s/cmdline' % child.pid).read_bytes().decode().split('\0')[:-1]
            if cg.ProcessManager.is_codex_client(parts):
                break
            time.sleep(.01)
        else:
            self.fail('Disposable codex child did not start')
        original_identity = cg.ProcessManager.process_identity
        reads = [0]
        def flaky_identity(pid):
            reads[0] += 1
            return None if reads[0] == 1 else original_identity(pid)
        with patch.object(cg.CodexSocketClient, 'query_rate_limits', return_value=healthy(99)), \
             patch.object(cg.ProcessManager, 'get_codex_client_pids', return_value=[(child.pid, 'S', 'codex')]), \
             patch.object(cg.ProcessManager, 'process_identity', side_effect=flaky_identity), \
             patch.object(cg.ProcessManager, 'suspend_pid', wraps=cg.ProcessManager.suspend_pid) as suspend, \
             patch.object(cg.SessionManager, 'get_loaded_thread_ids', return_value=[]), \
             patch.object(cg.SessionManager, 'interrupt_active_turns', return_value=[]), \
             patch.object(cg, 'send_notification'), patch('time.sleep', side_effect=KeyboardInterrupt):
            cg.run_watch_loop(auto_continue=False)
        suspend.assert_not_called()
        self.assertNotIn(cg.ProcessManager.read_stat(child.pid)[0], ('T', 't'))
        self.assertEqual(cg.read_snapshot()['active_pids'], [])


class AutomaticRecoveryResultsTests(IsolatedTest):
    def run_recovery(self, outcomes, sessions=False, second_poll=False, stop_during_resume=False,
                     already_running=False, interrupt_after_signal=False):
        pids = [55555, 55556] if second_poll or len(outcomes) > 1 else [55555]
        identities = {str(pid): {'pid': pid, 'start_time': 1} for pid in pids}
        cg.write_snapshot({'reason': 'quota_exceeded', 'active_pids': pids,
                           'process_identities': identities,
                           'sessions': [{'id': 'saved-task'}] if sessions else []})
        states = {pid: 'S' if already_running else 'T' for pid in pids}
        results = iter(outcomes)
        def resume_signal(pid, expected=None):
            self.assertEqual(expected, identities[str(pid)])
            if stop_during_resume:
                cg.write_snapshot({'reason': 'manual_top_pause', 'active_pids': []})
            ok = next(results)
            if ok:
                states[pid] = 'S'
            return ok
        poll_count = [0]
        def sleep(seconds):
            if interrupt_after_signal and seconds == 1:
                raise KeyboardInterrupt
            if seconds == 7:
                poll_count[0] += 1
                if second_poll and poll_count[0] == 1:
                    # The first client was successfully released, then paused by its user.
                    states[55555] = 'T'
                else:
                    raise KeyboardInterrupt
        with patch.object(cg.CodexSocketClient, 'query_rate_limits', return_value=healthy()), \
             patch.object(cg.ProcessManager, 'get_codex_client_pids', side_effect=lambda: [(pid, state, 'codex') for pid, state in states.items()]), \
             patch.object(cg.ProcessManager, 'process_identity', side_effect=lambda pid: identities[str(pid)]), \
             patch.object(cg.ProcessManager, 'resume_pid', side_effect=resume_signal) as resume, \
             patch.object(cg.SessionManager, 'get_loaded_thread_ids', return_value=[]), \
             patch.object(cg.SessionManager, 'get_root_thread_id', side_effect=lambda tid: tid), \
             patch.object(cg.SessionManager, 'is_subagent', return_value=False), \
             patch.object(cg.SessionManager, 'launch_codex_session', return_value=False) as launch, \
             patch.object(cg.SessionManager, 'restore_and_verify_sessions', return_value={'sessions': []}) as restore, \
             patch.object(cg, 'send_notification') as notify, patch('time.sleep', side_effect=sleep):
            cg.run_watch_loop(interval=7, auto_continue=False)
        return cg.read_snapshot(), resume, launch, restore, notify

    def test_partial_resume_retires_success_before_waiting(self):
        saved, resume, _, _, _ = self.run_recovery([True, False], interrupt_after_signal=True)
        self.assertEqual(resume.call_count, 2)
        self.assertEqual(saved['active_pids'], [55556])
        self.assertEqual(set(saved['process_identities']), {'55556'})
        self.assertEqual(saved['reason'], 'quota_exceeded')

    def test_retry_does_not_resume_user_pause_of_released_client(self):
        saved, resume, _, _, _ = self.run_recovery([True, False, True, True], second_poll=True)
        self.assertEqual([c.args[0] for c in resume.call_args_list], [55555, 55556, 55556])
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['reason'], 'quota_recovered')

    def test_failed_session_launch_does_not_retain_successful_process_ownership(self):
        saved, resume, launch, _, _ = self.run_recovery([True], sessions=True)
        resume.assert_called_once()
        launch.assert_called_once()
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['process_identities'], {})
        self.assertEqual(saved['reason'], 'quota_exceeded')
        self.assertEqual(saved['sessions'], [{'id': 'saved-task'}])

    def test_new_manual_pause_prevents_remaining_recovery_actions(self):
        saved, _, launch, restore, notify = self.run_recovery([True], stop_during_resume=True)
        self.assertEqual(saved['reason'], 'manual_top_pause')
        launch.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(any(c.args[0] == 'Codex 额度已恢复' for c in notify.call_args_list))

    def test_running_client_ownership_is_retired_even_if_session_launch_fails(self):
        saved, resume, _, _, _ = self.run_recovery([], sessions=True, already_running=True)
        resume.assert_not_called()
        self.assertEqual(saved['active_pids'], [])
        self.assertEqual(saved['reason'], 'quota_exceeded')

    def test_failed_resume_remains_pending(self):
        saved, resume, _, _, notify = self.run_recovery([False])
        resume.assert_called_once()
        self.assertEqual(saved['active_pids'], [55555])
        self.assertEqual(set(saved['process_identities']), {'55555'})
        self.assertEqual(saved['reason'], 'quota_exceeded')
        self.assertFalse(any(c.args[0] == 'Codex 额度已恢复' for c in notify.call_args_list))


if __name__ == '__main__':
    unittest.main()
