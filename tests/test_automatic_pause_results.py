"""Automatic pause ownership regressions; no host clients or RPCs are used."""
import unittest
from unittest.mock import patch

from test_security_regressions import IsolatedTest, cg, healthy


class AutomaticPauseResultsTests(IsolatedTest):
    def run_guard(self, quotas, clients, outcomes, sleeps=None, previous=None):
        if previous is not None:
            cg.write_snapshot(previous)
        with patch.object(cg.CodexSocketClient, 'query_rate_limits', side_effect=quotas), \
             patch.object(cg.ProcessManager, 'get_codex_client_pids', side_effect=clients), \
             patch.object(cg.ProcessManager, 'process_identity', side_effect=lambda pid: {'pid': pid, 'start_time': 1}), \
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


if __name__ == '__main__':
    unittest.main()
