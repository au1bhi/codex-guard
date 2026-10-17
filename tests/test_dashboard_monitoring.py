"""Monitoring regressions with fake RPC data and an isolated PTY reload."""
import io, os, pty, runpy, select, struct, subprocess, sys, tempfile, termios, fcntl, time, unittest
from pathlib import Path
from unittest.mock import patch

SOURCE=str(Path(__file__).resolve().parents[1] / 'bin' / 'codex-guard')
m=runpy.run_path(SOURCE,run_name='regression')
D=m['CodexTopDashboard'];S=m['SessionManager'];C=m['CodexSocketClient']

class Screen:
    def __init__(self):self.rows={}
    def addnstr(self,y,x,text,count,attr=0):
        old=self.rows.get(y,' '*180);self.rows[y]=old[:x]+text[:count]+old[x+len(text[:count]):]

def data():
    return dict(snapshot=None,processes=[],threads=[],service_active=False,service_pid='0',auto_continue_enabled=False,uptime='up',load='0',time_str='now')

class Regression(unittest.TestCase):
    def test_collector_failure_invalidates_previous_healthy_data(self):
        dash=D(delay=.001)
        previous=data()
        previous['snapshot']=m['QuotaSnapshot']({'rateLimits':{'primary':{'usedPercent':1,'windowDurationMins':300}}})
        dash._cached_data=previous
        def fail():
            dash._stop_event.set()
            raise RuntimeError('private error details\x1b[2J')
        with patch.object(dash,'collect_data',side_effect=fail):dash._background_collector()
        state=dash.get_snapshot()
        self.assertIsNone(state['snapshot']);self.assertFalse(state['loading'])
        self.assertEqual(state['collection_error'],'RuntimeError')
        output=io.StringIO();dash.render_batch_from_data(state,output)
        self.assertIn('采集失败',output.getvalue());self.assertIn('数据不可用',output.getvalue())
        self.assertNotIn('5h Quota:',output.getvalue());self.assertNotIn('private error',output.getvalue())
        self.assertNotIn('No active Codex',output.getvalue())

    def test_collector_recovers_after_failure(self):
        dash=D(delay=.001)
        healthy=data()
        calls=0
        def collect():
            nonlocal calls
            calls+=1
            if calls==1:
                raise RuntimeError('failure')
            dash._stop_event.set()
            self.assertEqual(dash._cached_data['collection_error'],'RuntimeError')
            return healthy
        with patch.object(dash,'collect_data',side_effect=collect):
            dash._background_collector()
        self.assertIs(dash.get_snapshot(),healthy)
        self.assertNotIn('collection_error',dash.get_snapshot())

    def test_synchronous_collection_failure_is_visible(self):
        dash=D()
        with patch.object(dash,'collect_data',side_effect=ValueError('untrusted response')):
            output=io.StringIO();dash.render_batch(output)
        self.assertIn('采集失败',output.getvalue())
        self.assertNotIn('untrusted response',output.getvalue())

    def test_curses_collection_failure_and_unknown_controls(self):
        state=D.unavailable_snapshot(error=RuntimeError())
        screen=Screen()
        with patch.object(m['curses'],'color_pair',return_value=0):
            D().draw_curses_screen(screen,state,30,180)
            self.assertIn('采集失败',screen.rows[1]);self.assertIn('数据不可用',screen.rows[6])
            small=Screen();D().draw_curses_screen(small,state,5,60)
        self.assertIn('UNKNOWN',small.rows[2]);self.assertIn('UNKNOWN',small.rows[3])

    def test_loading_does_not_claim_no_clients_exist(self):
        state=D.unavailable_snapshot(loading=True)
        output=io.StringIO();D().render_batch_from_data(state,output)
        self.assertIn('正在读取进程',output.getvalue())
        self.assertNotIn('No active Codex',output.getvalue())

    def test_ephemeral_metadata_only(self):
        reply={'thread':{'ephemeral':True,'status':{'type':'idle'},'name':'temporary','cwd':'/tmp'}}
        with patch.object(C,'query_app_server',return_value=reply) as rpc:
            state=S.get_live_thread_state('ephemeral')
        self.assertEqual(rpc.call_count,1)
        self.assertFalse(rpc.call_args.args[1]['includeTurns'])
        self.assertEqual(state['status_type'],'idle');self.assertIsNone(state['turns_count'])
        self.assertFalse(state['history_available']);self.assertEqual(state['title'],'temporary')

    def test_persistent_history(self):
        thread={'ephemeral':False,'status':{'type':'active'},'turns':[{'id':'last','status':'inProgress'}]}
        with patch.object(C,'query_app_server',side_effect=[{'thread':thread},{'thread':thread}]):
            state=S.get_live_thread_state('persistent')
        self.assertEqual(state['last_turn_id'],'last');self.assertEqual(state['turns_count'],1)

    def test_history_failure_preserves_status(self):
        with patch.object(C,'query_app_server',side_effect=[{'thread':{'status':{'type':'idle'}}},None,None]):
            state=S.get_live_thread_state('persistent')
        self.assertEqual(state['status_type'],'idle');self.assertIsNone(state['last_turn_id'])
        self.assertIsNone(state['turns_count'])

    def test_fallback_history_order(self):
        with patch.object(C,'query_app_server',side_effect=[{'thread':{'status':{'type':'idle'}}},None,{'data':[{'id':'new'},{'id':'old'}]}]):
            state=S.get_live_thread_state('persistent')
        self.assertEqual(state['last_turn_id'],'new');self.assertEqual([t['id'] for t in state['turns']],['old','new'])

    def test_exact_association_only(self):
        threads=[{'id':'AAA'},{'id':'BBB'}]
        self.assertIsNone(D.match_process_thread({'cmd':'codex'},threads))
        self.assertEqual(D.match_process_thread({'cmd':'codex resume BBB'},threads),threads[1])
        self.assertIsNone(D.match_process_thread({'cmd':'codex AAA BBB'},threads))

    def test_actual_summary(self):
        self.assertEqual(D.reasoning_summary({'summary':[{'text':'actual summary'}]}),'actual summary')
        self.assertEqual(D.reasoning_summary({'summary':[],'content':['actual content']}),'actual content')
        self.assertIn('未提供',D.reasoning_summary({}))
        self.assertNotIn('\x1b',D.reasoning_summary({'summary':['unsafe\x1b[2J']}))

    def test_quota_failure_and_recovery_clear_curses_rows(self):
        d=data();screen=Screen();dash=D()
        with patch.object(m['curses'],'color_pair',return_value=0):
            d['snapshot']=m['QuotaSnapshot']({'rateLimits':{'primary':{'usedPercent':2,'windowDurationMins':300},'secondary':{'usedPercent':10,'windowDurationMins':10080}}})
            dash.draw_curses_screen(screen,d,30,180);self.assertIn('Week Quota',screen.rows[2])
            d['snapshot']=None;dash.draw_curses_screen(screen,d,30,180)
            self.assertIn('额度不可用',screen.rows[1]);self.assertNotIn('Week Quota',screen.rows[2])
            d['snapshot']=m['QuotaSnapshot']({'rateLimits':{'primary':{'usedPercent':2,'windowDurationMins':300}}})
            dash.draw_curses_screen(screen,d,30,180);self.assertIn('5h Quota',screen.rows[1])
            self.assertNotIn('额度不可用',screen.rows[1])

    def test_small_terminal_failure_visible(self):
        screen=Screen()
        with patch.object(m['curses'],'color_pair',return_value=0):D().draw_curses_screen(screen,data(),5,30)
        self.assertIn('额度不可用',screen.rows[1])

    def test_cold_start_does_not_collect_twice(self):
        dash=D()
        with patch.object(dash,'collect_data',side_effect=AssertionError('duplicate synchronous collection')):
            with patch('threading.Thread.is_alive',return_value=True):
                import threading
                dash._worker_thread=threading.Thread();state=dash.get_snapshot()
        self.assertTrue(state['loading'])

    def test_unknown_alias_command_rejected(self):
        p=subprocess.run([sys.executable, str(Path(SOURCE).with_name('codex-top')), 'invalid-regression-command'],capture_output=True,timeout=5)
        self.assertEqual(p.returncode,2)

    def test_hot_reload_restores_ansi_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            script=directory+'/codex-top'
            source=Path(SOURCE).read_text()
            entrypoint = 'dashboard = CodexTopDashboard(delay=delay, continue_prompt=prompt)'
            self.assertIn(entrypoint, source)
            source = source.replace(entrypoint,
                entrypoint + '\n    dashboard.collect_data = lambda: ' + repr(data()), 1)
            Path(script).write_text(source)
            os.chmod(script,0o700)
            master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,160,0,0))
            before=termios.tcgetattr(slave);output=bytearray()
            p=subprocess.Popen([script],stdin=slave,stdout=slave,stderr=slave,env=dict(os.environ,TERM='invalid-audit-term'),start_new_session=True)
            def receive_until(predicate,timeout=10):
                end=time.monotonic()+timeout
                while time.monotonic()<end:
                    if select.select([master],[],[],.1)[0]:
                        try:output.extend(os.read(master,65536))
                        except OSError:break
                    if predicate():return
                self.fail('terminal condition timed out')
            try:
                receive_until(lambda:b'codex-top' in output)
                stamp=os.stat(script).st_mtime+2;os.utime(script,(stamp,stamp));os.write(master,b'r')
                receive_until(lambda:output.count(b'\x1b[?1049h')>=2 and b'codex-top' in output[output.rfind(b'\x1b[?1049h'):])
                os.write(master,b'q');p.wait(timeout=5)
                self.assertEqual(p.returncode,0);self.assertEqual(termios.tcgetattr(slave),before)
            finally:
                if p.poll() is None:p.kill();p.wait()
                os.close(master);os.close(slave)

if __name__=='__main__':unittest.main(verbosity=2)
