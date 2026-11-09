"""Privacy routing regressions. All model transports are local test fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import requests
from unittest.mock import MagicMock, patch

from beam_core import Agent, Run, Store
from crew import Crew
from desktop import create_app
from privacy_guard import PrivacyPause, detect_sensitive, _local_json

PRIVATE_IP = '192.168.1.47'
SECOND_IP = '10.21.34.56'
PRIVATE_EMAIL = 'beam-privacy-fixture@gmail.com'
LOCAL_MARKER = 'LOCAL-ONLY-ANALYSIS-OUTPUT'
LOCAL_MODEL = 'qwen3:4b'


class Response:
    status_code = 200
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_lines(self):
        yield json.dumps({'message': self.message, 'done': True}).encode()
    def close(self): pass
    def json(self): return {'message': self.message, 'done': True}
    def raise_for_status(self): pass


def tool(name, **arguments):
    return {'function': {'name': name, 'arguments': arguments}}


def until(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = predicate()
        if result: return result
        time.sleep(.01)
    raise AssertionError('Privacy workflow did not reach the expected state')


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'project'
        self.folder.mkdir()
        self.store = Store(self.root / 'data')
        self.store.save_settings({'reflection':'off'})  # review passes have their own tests
        self.agent = Agent(self.store)
        self.pid = self.store.add_project(self.folder)['id']
        self.sid = self.store.create_session(self.pid)['id']
        self.key_patch = patch.object(self.store, 'key', return_value='fixture-key')
        self.key_patch.start()
        self.cloud_payloads = []
        self.local_calls = []
        self.crew = None

    def tearDown(self):
        if self.crew: self.crew.close()
        for run in list(self.agent.runs.values()): run.stop()
        for run in list(self.agent.runs.values()):
            if run.worker: run.worker.join(3)
        self.agent.integrations.close()
        self.agent.servers.close()
        self.key_patch.stop()
        self.store.db.close()
        self.temp.cleanup()

    def local(self, method, path, payload=None, timeout=None, **kwargs):
        self.local_calls.append((method, path, deepcopy(payload)))
        if path.endswith('/tags'):
            return {'models': [{'name': LOCAL_MODEL, 'size': 123456789,
                                'details': {'parameter_size': '4B', 'family': 'qwen3'}}]}
        if path.endswith('/show'):
            return {'model_info': {'general.architecture': 'qwen3', 'general.parameter_count': 4000000000},
                    'details': {'family': 'qwen3', 'parameter_size': '4B', 'quantization_level': 'Q4_K_M'},
                    'capabilities': ['completion'], 'modelfile': 'FROM C:/fixture/sha256-weights'}
        if path.endswith('/chat'):
            return {'message': {'content': LOCAL_MARKER + ': the private address is ' + PRIVATE_IP}, 'done': True}
        raise AssertionError('Unexpected local endpoint: ' + path)

    def cloud(self, *args, **kwargs):
        self.cloud_payloads.append(deepcopy(kwargs['json']))
        return Response({'content': 'Public work completed.'})

    def pending(self, run):
        return until(lambda: getattr(run, 'privacy_pending', None) if run.status == 'privacy' else None)

    def local_until_done(self, run, sid=None, seconds=8):
        sid = sid or self.sid
        answered = set()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pending = getattr(run, 'privacy_pending', None)
            if run.status == 'privacy' and pending and pending['action_id'] not in answered:
                answered.add(pending['action_id'])
                self.agent.choose_privacy(sid, pending['action_id'], 'local', LOCAL_MODEL)
            if run.worker and not run.worker.is_alive():
                return run.status
            time.sleep(.01)
        raise AssertionError('Local privacy continuation did not finish')

    def assert_cloud_private_free(self):
        serialized = json.dumps(self.cloud_payloads)
        self.assertNotIn(PRIVATE_IP, serialized)
        self.assertNotIn(LOCAL_MARKER, serialized)

    def test_normal_coding_uses_beam_cloud_without_privacy_prompt(self):
        with patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Create a simple Flask hello page.', 'edit')
            until(lambda: run.worker and not run.worker.is_alive())
        self.assertEqual(run.status, 'done')
        self.assertEqual(len(self.cloud_payloads), 1)
        self.assertEqual(self.cloud_payloads[0]['model'], 'beam')
        self.assertFalse(any(e['type'] == 'privacy' for e in self.store.events(self.sid)))

    def test_private_user_value_gates_before_first_cloud_request(self):
        with patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Troubleshoot my connection at ' + PRIVATE_IP, 'ask')
            pending = self.pending(run)
            self.assertEqual(self.cloud_payloads, [])
            self.assertFalse(run.snapshot()['working'])
            self.assertNotIn(PRIVATE_IP, json.dumps(pending))
            gate_events = [e for e in self.store.events(self.sid) if e['type'] == 'privacy']
            self.assertNotIn(PRIVATE_IP, json.dumps(gate_events))
            self.assertNotIn(PRIVATE_IP, json.dumps(self.agent.run_state(self.sid)))
            run.stop()
            run.worker.join(2)
        self.assertEqual(run.status, 'stopped')
        self.assertEqual(self.cloud_payloads, [])

    def test_personal_ip_request_gates_before_any_model_call(self):
        with patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Find my public IP address and explain it.', 'ask')
            self.pending(run)
            self.assertEqual(self.cloud_payloads, [])
            run.stop()
            run.worker.join(2)
        self.assertEqual(run.status, 'stopped')

    def test_local_summary_and_private_value_never_enter_later_cloud_turns(self):
        with patch('privacy_guard._local_json', side_effect=self.local), \
                patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Review connection details: ' + PRIVATE_IP, 'ask')
            self.assertEqual(self.local_until_done(run), 'done')
            summaries = [e for e in self.store.events(self.sid) if e['type'] == 'private_summary']
            self.assertTrue(summaries)
            self.assertIn(LOCAL_MARKER, summaries[-1]['text'])
            followup = self.agent.start(self.sid, 'Now write a public troubleshooting outline.', 'ask')
            self.assertEqual(self.local_until_done(followup), 'done')
        self.assertTrue(self.cloud_payloads)
        self.assert_cloud_private_free()
        private_messages = self.store.session(self.sid)['messages']
        self.assertNotIn(LOCAL_MARKER, json.dumps(private_messages))
        self.assertTrue(any(path.endswith('/chat') for _, path, _ in self.local_calls))

    def test_new_sensitive_tool_result_gates_before_next_cloud_step(self):
        (self.folder / 'network.txt').write_text('Connection address: ' + PRIVATE_IP, 'utf-8')
        def model(*args, **kwargs):
            self.cloud_payloads.append(deepcopy(kwargs['json']))
            if not any(m.get('tool_name') == 'read_file' for m in kwargs['json']['messages']):
                return Response({'tool_calls': [tool('read_file', path='network.txt')]})
            return Response({'content': 'Private check completed locally.'})
        with patch('privacy_guard._local_json', side_effect=self.local), \
                patch('beam_core.requests.post', side_effect=model):
            run = self.agent.start(self.sid, 'Inspect network.txt and report completion.', 'ask')
            self.pending(run)
            self.assertEqual(len(self.cloud_payloads), 1)
            self.assertEqual(self.local_until_done(run), 'done')
        self.assertEqual(len(self.cloud_payloads), 2)
        self.assert_cloud_private_free()

    def test_explicit_cloud_consent_is_value_scoped_and_new_values_gate(self):
        (self.folder / 'new-contact.txt').write_text('Contact: ' + PRIVATE_EMAIL, 'utf-8')
        def model(*args, **kwargs):
            self.cloud_payloads.append(deepcopy(kwargs['json']))
            return Response({'tool_calls': [tool('read_file', path='new-contact.txt')]})
        with patch('beam_core.requests.post', side_effect=model):
            run = self.agent.start(self.sid, 'Check connection ' + PRIVATE_IP, 'ask')
            first = self.pending(run)
            self.agent.choose_privacy(self.sid, first['action_id'], 'cloud')
            second = until(lambda: getattr(run, 'privacy_pending', None)
                           if run.status == 'privacy' and getattr(run, 'privacy_pending', None)
                           and run.privacy_pending['action_id'] != first['action_id'] else None)
            self.assertNotEqual(first['action_id'], second['action_id'])
            self.assertEqual(len(self.cloud_payloads), 1)
            self.assertIn(PRIVATE_IP, json.dumps(self.cloud_payloads[0]))
            self.assertNotIn(PRIVATE_EMAIL, json.dumps(self.cloud_payloads))
            self.assertNotIn(PRIVATE_EMAIL, json.dumps(second))
            run.stop()
            run.worker.join(2)
        self.assertEqual(run.status, 'stopped')

    def test_stale_privacy_decision_is_rejected(self):
        with patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Connection details: ' + PRIVATE_IP, 'ask')
            pending = self.pending(run)
            with self.assertRaises(ValueError):
                self.agent.choose_privacy(self.sid, 'stale-action-id', 'cloud')
            self.assertEqual(self.cloud_payloads, [])
            self.agent.choose_privacy(self.sid, pending['action_id'], 'cloud')
            until(lambda: run.worker and not run.worker.is_alive())
            with self.assertRaises(ValueError):
                self.agent.choose_privacy(self.sid, pending['action_id'], 'cloud')
        self.assertEqual(len(self.cloud_payloads), 1)

    def test_missing_local_model_never_falls_back_to_cloud(self):
        def no_models(method, path, payload=None, timeout=None, **kwargs):
            return {'models': []}
        with patch('privacy_guard._local_json', side_effect=no_models), \
                patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Connection details: ' + PRIVATE_IP, 'ask')
            pending = self.pending(run)
            try:
                self.agent.choose_privacy(self.sid, pending['action_id'], 'local', LOCAL_MODEL)
            except ValueError:
                pass
            until(lambda: run.worker and not run.worker.is_alive())
            self.assertEqual(run.status, 'needs_attention')
            self.assertEqual(self.cloud_payloads, [])
        self.assertFalse(run.snapshot()['working'])

    def test_local_transport_failure_never_falls_back_to_cloud(self):
        def unavailable(method, path, payload=None, timeout=None, **kwargs):
            if path.endswith('/tags') or path.endswith('/show'): return self.local(method, path, payload, timeout)
            raise PrivacyPause('Local Ollama is unavailable')
        with patch('privacy_guard._local_json', side_effect=unavailable), \
                patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Connection details: ' + PRIVATE_IP, 'ask')
            pending = self.pending(run)
            try:
                self.agent.choose_privacy(self.sid, pending['action_id'], 'local', LOCAL_MODEL)
            except ValueError:
                pass
            until(lambda: run.worker and not run.worker.is_alive())
            self.assertEqual(run.status, 'needs_attention')
            self.assertEqual(self.cloud_payloads, [])
        self.assertFalse(run.snapshot()['working'])

    def test_crew_local_analysis_is_not_returned_to_cloud_lead(self):
        (self.folder / 'network.txt').write_text('Connection address: ' + PRIVATE_IP, 'utf-8')
        self.crew = Crew(self.store, self.agent)
        def model(*args, **kwargs):
            payload = kwargs['json']
            self.cloud_payloads.append(deepcopy(payload))
            names = {t['function']['name'] for t in payload['tools']}
            previous = {m.get('tool_name') for m in payload['messages'] if m.get('role') == 'tool'}
            if 'crew_delegate' in names:
                if 'crew_delegate' not in previous:
                    return Response({'tool_calls': [tool('crew_delegate', bot_id='engineer', title='Inspect notes',
                                                          prompt='Read network.txt and report completion.')]})
                return Response({'content': 'Team review completed.'})
            if 'read_file' not in previous:
                return Response({'tool_calls': [tool('read_file', path='network.txt')]})
            return Response({'content': 'Private check completed locally.'})
        with patch('privacy_guard._local_json', side_effect=self.local), \
                patch('beam_core.requests.post', side_effect=model):
            task = self.crew.create_task(self.pid, {'prompt': 'Audit the project notes.', 'mode': 'ask'})
            end = time.monotonic() + 10
            seen = set()
            gated = False
            while time.monotonic() < end:
                for sid, run in list(self.agent.runs.items()):
                    pending = getattr(run, 'privacy_pending', None)
                    if run.status == 'privacy' and pending and pending['action_id'] not in seen:
                        gated = True
                        seen.add(pending['action_id'])
                        self.assertFalse(self.crew.state(self.pid)['working'])
                        self.agent.choose_privacy(sid, pending['action_id'], 'local', LOCAL_MODEL)
                state = self.crew.state(self.pid)
                original = next(t for t in state['tasks'] if t['id'] == task['id'])
                if original['status'] == 'done': break
                time.sleep(.01)
            self.assertEqual(original['status'], 'done')
            self.assertTrue(gated)
        self.assert_cloud_private_free()
        self.assertTrue(any('TEAM RESULTS' in json.dumps(payload) for payload in self.cloud_payloads))

    def test_detector_matches_personal_values_and_ignores_documentation_addresses(self):
        secret = 'ghp_' + 'Q' * 36
        findings = detect_sensitive({'text': PRIVATE_IP + ' ' + PRIVATE_EMAIL + ' ' + secret})
        values = {f['value'] for f in findings}
        self.assertIn(PRIVATE_IP, values)
        self.assertIn(PRIVATE_EMAIL, values)
        self.assertIn(secret, values)
        examples = detect_sensitive({'text': 'Bind 127.0.0.1, 0.0.0.0 or ::1. Example 192.0.2.1, 198.51.100.8, 203.0.113.22 and 2001:db8::4.'})
        self.assertEqual(examples, [])


    def test_cloud_alias_is_never_used_for_local_private_analysis(self):
        for candidate in ('beam:cloud', 'qwen3-cloud', 'custom:cloud'):
            calls = []
            def aliases(method, path, payload=None, timeout=None, **kwargs):
                calls.append((method, path, deepcopy(payload)))
                if path.endswith('/tags'):
                    return {'models': [{'name': candidate, 'size': 123456789}]}
                if path.endswith('/show'):
                    return {'model_info': {'general.architecture': 'qwen3'}, 'remote_host': 'https://ollama.com', 'remote_model': 'beam'}
                if path.endswith('/chat'):
                    raise AssertionError('Private data reached a cloud-backed local alias')
                raise AssertionError(path)
            sid = self.store.create_session(self.pid)['id']
            with self.subTest(model=candidate), patch('privacy_guard._local_json', side_effect=aliases), \
                    patch('beam_core.requests.post', side_effect=self.cloud):
                run = self.agent.start(sid, 'Connection details: ' + PRIVATE_IP, 'ask')
                pending = self.pending(run)
                try:
                    self.agent.choose_privacy(sid, pending['action_id'], 'local', candidate)
                except ValueError:
                    pass
                time.sleep(.1)
                self.assertFalse(any(path.endswith('/chat') for _, path, _ in calls))
                self.assertEqual(self.cloud_payloads, [])
                run.stop()
                run.worker.join(2)

    def test_disguised_remote_model_metadata_refuses_local_private_chat(self):
        calls = []
        def disguised(method, path, payload=None, timeout=None, **kwargs):
            calls.append((method, path, deepcopy(payload)))
            if path.endswith('/tags'):
                return {'models': [{'name': LOCAL_MODEL, 'size': 123456789}]}
            if path.endswith('/show'):
                return {'model_info': {'general.architecture': 'qwen3'},
                        'remote_host': 'https://ollama.com', 'remote_model': 'beam'}
            if path.endswith('/chat'):
                raise AssertionError('Private data reached disguised remote model')
            raise AssertionError(path)
        with patch('privacy_guard._local_json', side_effect=disguised), \
                patch('beam_core.requests.post', side_effect=self.cloud):
            run = self.agent.start(self.sid, 'Connection details: ' + PRIVATE_IP, 'ask')
            pending = self.pending(run)
            try:
                self.agent.choose_privacy(self.sid, pending['action_id'], 'local', LOCAL_MODEL)
            except ValueError:
                pass
            time.sleep(.1)
            self.assertFalse(any(path.endswith('/chat') for _, path, _ in calls))
            self.assertEqual(self.cloud_payloads, [])
            run.stop()
            run.worker.join(2)

    def test_json_secret_field_names_and_private_data_keys_are_detected_and_redacted(self):
        ip_key = '10.23.4.5'
        mail_key = 'private-key-fixture@gmail.com'
        secret = 'arbitrary-unprefixed-secret'
        document = {'password': 'hunter2', 'nested': {'api_key': secret}, ip_key: 'host', mail_key: 'contact'}
        findings = detect_sensitive({'messages': [{'role': 'tool', 'tool_name': 'read_file',
                                                   'content': json.dumps(document)}]})
        values = {f['value'] for f in findings}
        for value in ('hunter2', secret, ip_key, mail_key):
            self.assertIn(value, values)
        from beam_core import Run
        run = Run()
        payload = {'model': 'beam', 'messages': [{'role': 'tool', 'tool_name': 'read_file',
                                                'content': json.dumps(document)}]}
        with patch('privacy_guard._local_json', side_effect=self.local):
            safe = self.agent.privacy.prepare(payload, run, lambda categories, preview: ('local', LOCAL_MODEL))
        serialized = json.dumps(safe)
        for value in ('hunter2', secret, ip_key, mail_key):
            self.assertNotIn(value, serialized)
        self.assertEqual(safe['model'], 'beam')
        result = next(m for m in safe['messages'] if m['role'] == 'tool')
        self.assertEqual(result['tool_name'], 'read_file')
        parsed = json.loads(result['content'])
        self.assertEqual(len(parsed), len(document))
        self.assertIn('password', parsed)

    def test_unverified_local_model_weights_fail_closed(self):
        for metadata in ({}, {'model_info': {'general.architecture': 'qwen3'}},
                         {'model_info': {'general.architecture': 'qwen3', 'general.parameter_count': 4000000000},
                          'nested': {'remote_model': 'beam'}}):
            calls = []
            def unknown(method, path, payload=None, timeout=None, **kwargs):
                calls.append((method, path, deepcopy(payload)))
                if path.endswith('/tags'):
                    return {'models': [{'name': LOCAL_MODEL, 'size': 123456789}]}
                if path.endswith('/show'): return metadata
                raise AssertionError('Unverified model was given private data')
            with self.subTest(metadata=metadata), patch('privacy_guard._local_json', side_effect=unknown):
                with self.assertRaises(PrivacyPause):
                    self.agent.privacy._model(LOCAL_MODEL)
            self.assertFalse(any(path.endswith('/chat') for _, path, _ in calls))

    def test_concurrent_privacy_decisions_accept_exactly_one_choice(self):
        run = Run()
        run.status = 'privacy'
        run.privacy_pending = {'action_id': 'privacy-race-fixture'}
        self.agent.runs[self.sid] = run
        entered = threading.Event()
        attempted_second = threading.Event()
        release = threading.Event()
        accepted, rejected = [], []
        original_set = run.privacy_event.set
        def slow_set():
            entered.set()
            release.wait(3)
            original_set()
        def choose(label):
            if label == 'second': attempted_second.set()
            try:
                self.agent.choose_privacy(self.sid, 'privacy-race-fixture', 'cloud')
                accepted.append(label)
            except ValueError:
                rejected.append(label)
        first = threading.Thread(target=choose, args=('first',))
        second = threading.Thread(target=choose, args=('second',))
        try:
            with patch.object(run.privacy_event, 'set', side_effect=slow_set) as setter:
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertTrue(attempted_second.wait(1))
                time.sleep(.05)
                self.assertEqual(setter.call_count, 1, 'A second decision passed the stale-action check during the first decision')
                release.set()
                first.join(2)
                second.join(2)
            self.assertEqual(len(accepted), 1)
            self.assertEqual(len(rejected), 1)
        finally:
            release.set()
            if first.ident is not None: first.join(2)
            if second.ident is not None: second.join(2)
            run.status = 'done'

    def test_unclosed_private_marker_cannot_reach_cloud(self):
        private_text = 'Never-public-private-fixture-text'
        for opening in ('[private]', '<private>'):
            sid = self.store.create_session(self.pid)['id']
            with self.subTest(marker=opening), patch('beam_core.requests.post', side_effect=self.cloud):
                run = self.agent.start(sid, 'Inspect this note: ' + opening + private_text, 'ask')
                until(lambda: run.status == 'privacy' or (run.worker and not run.worker.is_alive()))
                self.assertIn(run.status, ('privacy', 'needs_attention'))
                self.assertEqual(self.cloud_payloads, [])
                self.assertNotIn(private_text, json.dumps(getattr(run, 'privacy_pending', None)))
                run.stop()
                run.worker.join(2)
            self.assertFalse(run.snapshot()['working'])

    def test_punctuated_private_prompt_values_gate_before_cloud_and_redact_after_local(self):
        values = ('10.23.45.67', '2606:4700:4700::1111', 'beam-punctuation-fixture@gmail.com')
        for value in values:
            for prefix, suffix in (('', '.'), ('', ','), ('', ':'), ('(', ')')):
                self.cloud_payloads = []
                sid = self.store.create_session(self.pid)['id']
                with self.subTest(value=value, punctuation=prefix + suffix), \
                        patch('privacy_guard._local_json', side_effect=self.local), \
                        patch('beam_core.requests.post', side_effect=self.cloud):
                    run = self.agent.start(sid, 'Analyze the observed value ' + prefix + value + suffix, 'ask')
                    until(lambda: run.status == 'privacy' or (run.worker and not run.worker.is_alive()))
                    self.assertEqual(run.status, 'privacy', 'Sentence punctuation bypassed the first cloud checkpoint')
                    self.assertEqual(self.cloud_payloads, [])
                    self.assertEqual(self.local_until_done(run, sid), 'done')
                serialized = json.dumps(self.cloud_payloads)
                self.assertNotIn(value, serialized)
                self.assertNotIn(LOCAL_MARKER, serialized)
                self.assertNotIn(PRIVATE_IP, serialized)

    def test_punctuated_tool_results_gate_before_next_cloud_and_redact_after_local(self):
        values = ('10.23.45.67', '2606:4700:4700::1111', 'beam-punctuation-fixture@gmail.com')
        def model(*args, **kwargs):
            self.cloud_payloads.append(deepcopy(kwargs['json']))
            if not any(m.get('tool_name') == 'read_file' for m in kwargs['json']['messages']):
                return Response({'tool_calls': [tool('read_file', path='facts.txt')]})
            return Response({'content': 'Private findings were reviewed locally.'})
        for value in values:
            for prefix, suffix in (('', '.'), ('', ','), ('', ':'), ('(', ')')):
                self.cloud_payloads = []
                (self.folder / 'facts.txt').write_text('Observed value ' + prefix + value + suffix, 'utf-8')
                sid = self.store.create_session(self.pid)['id']
                with self.subTest(value=value, punctuation=prefix + suffix), \
                        patch('privacy_guard._local_json', side_effect=self.local), \
                        patch('beam_core.requests.post', side_effect=model):
                    run = self.agent.start(sid, 'Inspect facts.txt and report completion.', 'ask')
                    until(lambda: run.status == 'privacy' or (run.worker and not run.worker.is_alive()))
                    self.assertEqual(run.status, 'privacy', 'Sentence punctuation bypassed the tool-result checkpoint')
                    self.assertEqual(len(self.cloud_payloads), 1)
                    self.assertEqual(self.local_until_done(run, sid), 'done')
                serialized = json.dumps(self.cloud_payloads)
                self.assertNotIn(value, serialized)
                self.assertNotIn(LOCAL_MARKER, serialized)
                self.assertNotIn(PRIVATE_IP, serialized)

    def test_server_result_punctuation_is_detected_without_partial_version_matches(self):
        for value in ('10.23.45.67', '2606:4700:4700::1111', 'beam-punctuation-fixture@gmail.com'):
            for prefix, suffix in (('', '.'), ('', ','), ('', ':'), ('(', ')')):
                output = json.dumps({'output': 'Observed value ' + prefix + value + suffix, 'running': True})
                findings = detect_sensitive({'messages': [{'role': 'tool', 'tool_name': 'server_status', 'content': output}]})
                with self.subTest(value=value, punctuation=prefix + suffix):
                    self.assertIn(value, {finding['value'] for finding in findings})
        findings = detect_sensitive({'messages': [{'role': 'tool', 'tool_name': 'server_status',
                                                   'content': json.dumps({'output': 'Version 1.2.3.4.5 and 10.23.45.67.89 are version strings.'})}]})
        self.assertFalse(any(finding['category'] == 'IP address' for finding in findings))

class LocalTransportTests(unittest.TestCase):
    def session(self, status=200):
        session = MagicMock()
        session.__enter__.return_value = session
        response = MagicMock()
        response.status_code = status
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps({'message': {'content': 'Local result'}}).encode()]
        response.headers = {'Location': 'https://outside.example/collect'} if status == 307 else {}
        response.json.return_value = {'message': {'content': 'Local result'}}
        response.raise_for_status.return_value = None
        session.request.return_value = response
        session.get.return_value = response
        session.post.return_value = response
        return session

    def test_local_private_chat_is_fixed_loopback_without_proxy_or_redirect(self):
        session = self.session()
        payload = {'model': LOCAL_MODEL, 'messages': [{'role': 'user', 'content': PRIVATE_IP}]}
        with patch('privacy_guard.requests.Session', return_value=session):
            _local_json('POST', '/api/chat', payload)
        calls = session.request.call_args_list
        if calls:
            args, kwargs = calls[0]
            url = kwargs.get('url', args[1] if len(args) > 1 else None)
        else:
            args, kwargs = session.post.call_args
            url = kwargs.get('url', args[0] if args else None)
        self.assertEqual(url, 'http://127.0.0.1:11434/api/chat')
        self.assertIs(kwargs.get('allow_redirects'), False)
        proxy_settings = kwargs.get('proxies', {})
        self.assertTrue(session.trust_env is False or
                        (proxy_settings.get('http') == '' and proxy_settings.get('https') == ''),
                        'Local private requests must bypass environment HTTP proxies')
        self.assertEqual(kwargs.get('json'), payload)

    def test_local_redirect_is_rejected_without_following_location(self):
        session = self.session(307)
        with patch('privacy_guard.requests.Session', return_value=session):
            with self.assertRaises((ValueError, RuntimeError, requests.RequestException)):
                _local_json('POST', '/api/chat', {'model': LOCAL_MODEL, 'messages': [{'role': 'user', 'content': PRIVATE_IP}]})
        calls = session.request.call_args_list or session.post.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].kwargs.get('allow_redirects'), False)

    def test_external_endpoint_path_is_rejected_before_transport(self):
        session = self.session()
        with patch('privacy_guard.requests.Session', return_value=session):
            with self.assertRaises(ValueError):
                _local_json('POST', 'https://outside.example/api/chat', {'message': PRIVATE_IP})
        self.assertEqual(session.request.call_count + session.post.call_count, 0)


class PrivacyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        folder = self.root / 'project'
        folder.mkdir()
        self.app = create_app(self.root / 'data', 'privacy-fixture-token')
        self.client = self.app.test_client()
        self.headers = {'X-Beam-Token': 'privacy-fixture-token'}
        self.pid = self.app.store.add_project(folder)['id']
        self.sid = self.app.store.create_session(self.pid)['id']
        self.key_patch = patch.object(self.app.store, 'key', return_value='private-api-key-fixture')
        self.key_patch.start()

    def tearDown(self):
        self.app.crew.close()
        for run in list(self.app.agent.runs.values()):
            run.stop()
            if run.worker: run.worker.join(2)
        self.app.agent.integrations.close()
        self.app.agent.servers.close()
        self.key_patch.stop()
        self.app.store.db.close()
        self.temp.cleanup()

    def get(self, path): return self.client.get('/api/' + path, headers=self.headers)
    def post(self, path, body): return self.client.post('/api/' + path, json=body, headers=self.headers)

    def test_privacy_routes_require_token_and_saved_model_setting_is_private(self):
        self.assertEqual(self.client.get('/api/privacy/models').status_code, 403)
        self.assertEqual(self.client.post('/api/privacy/settings', json={'model': LOCAL_MODEL}).status_code, 403)
        self.assertEqual(self.post('privacy/settings', {'model': LOCAL_MODEL}).status_code, 200)
        self.assertEqual(self.app.store.settings()['privacy_local_model'], LOCAL_MODEL)
        with patch('privacy_guard._local_json', return_value={'models': [{'name': LOCAL_MODEL, 'size': 123456789},
                                                                        {'name': 'beam:cloud', 'size': 123456789}]}):
            models = self.get('privacy/models')
        self.assertEqual(models.status_code, 200)
        self.assertEqual([m['name'] for m in models.json['models']], [LOCAL_MODEL])
        self.assertEqual(models.json['model'], LOCAL_MODEL)
        self.assertNotIn(b'private-api-key-fixture', models.data)
        self.assertEqual(self.post('privacy/settings', {'model': 'bad\nmodel'}).status_code, 400)
        self.assertEqual(self.post('privacy/settings', {'model': []}).status_code, 400)

    def test_pending_gate_status_api_hides_values_and_stop_releases_worker(self):
        with patch('beam_core.requests.post') as cloud:
            run = self.app.agent.start(self.sid, 'Check connection: ' + PRIVATE_IP, 'ask')
            pending = until(lambda: getattr(run, 'privacy_pending', None) if run.status == 'privacy' else None)
            detail = self.get('sessions/' + self.sid)
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json['status'], 'privacy')
            self.assertFalse(detail.json['working'])
            self.assertNotIn(PRIVATE_IP, json.dumps({key: detail.json[key] for key in ('status', 'label', 'phase', 'privacy_pending')}))
            self.assertEqual(detail.json['privacy_pending']['action_id'], pending['action_id'])
            api_state = self.get('state')
            self.assertEqual(api_state.json['active'][self.sid], 'privacy')
            self.assertNotIn(b'private-api-key-fixture', api_state.data)
            self.assertNotIn('messages', detail.json)
            stale = self.post('sessions/' + self.sid + '/privacy', {'action_id': 'wrong', 'choice': 'cloud'})
            self.assertEqual(stale.status_code, 400)
            response = self.post('sessions/' + self.sid + '/privacy', {'action_id': pending['action_id'], 'choice': 'stop'})
            self.assertEqual(response.status_code, 200)
            run.worker.join(2)
            self.assertEqual(run.status, 'stopped')
            self.assertEqual(cloud.call_count, 0)

    def test_crew_privacy_blocks_same_project_code_until_resolved(self):
        with patch('beam_core.requests.post') as cloud:
            task = self.app.crew.create_task(self.pid, {'prompt': 'Check connection: ' + PRIVATE_IP, 'mode': 'ask'})
            detail = until(lambda: self.app.crew.task(task['id'])
                           if self.app.crew.task(task['id'])['task']['status'] == 'privacy' else None)
            self.assertEqual(detail['pending']['type'], 'privacy')
            self.assertFalse(detail['run']['working'])
            self.assertNotIn(PRIVATE_IP, json.dumps(detail['pending']))
            response = self.post('sessions/' + self.sid + '/run', {'prompt': 'Start Code while Crew is waiting', 'mode': 'edit'})
            self.assertEqual(response.status_code, 400)
            self.app.crew.stop_task(task['id'])
            until(lambda: self.app.crew.task(task['id'])['task']['status'] == 'stopped')
            self.assertEqual(cloud.call_count, 0)


if __name__ == '__main__': unittest.main()
