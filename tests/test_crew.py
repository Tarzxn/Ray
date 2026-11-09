"""Crew workflow regressions. Model responses are local fixtures, never live requests."""
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from beam_core import Agent, Run, Store
from crew import Crew
from desktop import create_app, NativeBridge


class Response:
    status_code = 200

    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_lines(self):
        yield json.dumps({'message': self.message, 'done': True}).encode()

    def close(self):
        pass


def tool(name, **arguments):
    return {'function': {'name': name, 'arguments': arguments}}


def until(predicate, seconds=6):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Crew did not reach the expected state before the timeout')


class CrewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'project'
        self.folder.mkdir()
        self.store = Store(self.root / 'data')
        self.store.save_settings({'reflection':'off'})  # review passes have their own tests
        self.agent = Agent(self.store)
        self.project = self.store.add_project(self.folder)
        self.pid = self.project['id']
        self.crew = Crew(self.store, self.agent)
        self.key_patch = patch.object(self.store, 'key', return_value='fixture-key')
        self.key_patch.start()

    def tearDown(self):
        self.crew.close()
        for run in self.agent.runs.values():
            run.stop()
        for run in self.agent.runs.values():
            if run.worker:
                run.worker.join(3)
        self.agent.integrations.close()
        self.agent.servers.close()
        self.key_patch.stop()
        self.store.db.close()
        self.temp.cleanup()

    def create(self, **data):
        return self.crew.create_task(self.pid, {'title': 'A useful task', 'prompt': 'Complete the task',
                                              'bot_id': 'lead', 'start': False, **data})

    def current(self, ident):
        return self.crew.task(ident)['task']

    def finished(self, ident, status='done'):
        return until(lambda: self.current(ident) if self.current(ident)['status'] == status else None)

    def test_default_team_and_task_sessions_are_distinct(self):
        code = self.store.create_session(self.pid)
        one = self.create()
        two = self.create(bot_id='engineer')
        self.assertNotEqual(one['session_id'], two['session_id'])
        self.assertNotIn(code['id'], self.crew.session_ids())
        self.assertIn(one['session_id'], self.crew.session_ids())
        self.assertEqual({b['id'] for b in self.crew.state(self.pid)['bots']},
                         {'lead', 'engineer', 'researcher', 'designer', 'analyst', 'writer'})

    def test_profiles_brief_and_tasks_survive_reopen(self):
        self.crew.save_bot({'id': 'engineer', 'name': 'Morgan', 'role': 'Engineer',
                            'instructions': 'Use readable code.', 'memory': 'Project uses Flask.',
                            'color': '#67a89a'})
        self.crew.save_brief(self.pid, 'Build a small local dashboard.')
        original = self.create(bot_id='engineer', title='Dashboard')
        self.crew.close()
        self.crew = Crew(self.store, self.agent)
        state = self.crew.state(self.pid)
        engineer = next(b for b in state['bots'] if b['id'] == 'engineer')
        self.assertEqual(engineer['name'], 'Morgan')
        self.assertEqual(engineer['memory'], 'Project uses Flask.')
        self.assertEqual(state['brief'], 'Build a small local dashboard.')
        self.assertEqual(self.current(original['id'])['session_id'], original['session_id'])
        self.assertEqual(self.current(original['id'])['status'], 'draft')

    def test_scoped_context_and_tools_do_not_leak_into_code(self):
        self.crew.save_brief(self.pid, 'A dashboard for a bakery.')
        self.crew.save_bot({'id': 'engineer', 'name': 'Morgan', 'role': 'Engineer',
                            'instructions': 'Write readable Python.', 'memory': 'Use Flask.', 'color': '#67a89a'})
        task = self.create(bot_id='engineer')
        with patch('beam_core.requests.post', return_value=Response({'content': 'Reviewed the task.'})) as posted:
            self.crew.start_task(task['id'])
            self.finished(task['id'])
            code = self.store.create_session(self.pid)
            run = self.agent.start(code['id'], 'Explain the project', 'plan')
            until(lambda: run.status == 'done')
        crew_payload = posted.call_args_list[0].kwargs['json']
        code_payload = posted.call_args_list[-1].kwargs['json']
        context = crew_payload['messages'][0]['content']
        self.assertIn('Morgan', context)
        self.assertIn('Write readable Python.', context)
        self.assertIn('A dashboard for a bakery.', context)
        self.assertIn('Use Flask.', context)
        crew_tools = {t['function']['name'] for t in crew_payload['tools']}
        code_tools = {t['function']['name'] for t in code_payload['tools']}
        self.assertIn('crew_remember', crew_tools)
        self.assertNotIn('crew_delegate', crew_tools)
        self.assertFalse(any(name.startswith('crew_') for name in code_tools))
        self.assertNotIn('Morgan', code_payload['messages'][0]['content'])
        self.assertEqual([e['text'] for e in self.store.events(code['id']) if e['type'] == 'user'],
                         ['Explain the project'])

    def test_lead_delegates_real_child_and_receives_result(self):
        def model(*args, **kwargs):
            payload = kwargs['json']
            names = {t['function']['name'] for t in payload['tools']}
            previous = payload['messages']
            results = [m.get('tool_name') for m in previous if m.get('role') == 'tool']
            if 'crew_delegate' in names:
                if 'crew_delegate' not in results:
                    return Response({'content': 'Assigning implementation.', 'tool_calls': [
                        tool('crew_delegate', bot_id='engineer', title='Write the result',
                             prompt='Write crew-result.txt containing finished.')]})
                return Response({'content': 'Team result reviewed.'})
            if 'write_file' not in results:
                return Response({'content': 'Creating the requested result.', 'tool_calls': [
                    tool('write_file', path='crew-result.txt', content='finished')]})
            return Response({'content': 'Created crew-result.txt with finished.'})
        root = self.create(mode='edit')
        with patch('beam_core.requests.post', side_effect=model) as posted:
            self.crew.start_task(root['id'])
            until(lambda: len(self.crew.state(self.pid)['tasks']) == 2)
            self.finished(root['id'])
        self.assertEqual((self.folder / 'crew-result.txt').read_text('utf-8'), 'finished')
        children = [t for t in self.crew.state(self.pid)['tasks'] if t['parent_id'] == root['id']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['bot_id'], 'engineer')
        self.assertEqual(children[0]['mode'], 'edit')
        self.assertEqual(children[0]['status'], 'done')
        events = self.store.events(root['session_id'])
        self.assertTrue(any('crew-result.txt' in json.dumps(e) for e in events))
        # Automatic result synthesis is a system handoff, not a new user request.
        user_messages = [e['text'] for e in events if e['type'] == 'user']
        self.assertEqual(user_messages, ['Complete the task'])
        self.assertFalse(any('TEAM RESULTS' in text or 'Your delegated team tasks have finished' in text
                             for text in user_messages))
        review_messages = [m['content'] for call in posted.call_args_list
                           for m in call.kwargs['json']['messages']
                           if m.get('role') == 'user' and 'TEAM RESULTS (JSON)' in m.get('content', '')]
        self.assertTrue(review_messages, 'The Lead must still receive actual child results for review')
        self.assertIn('Created crew-result.txt with finished.', review_messages[-1])
        self.assertIn('crew-result.txt', review_messages[-1])

    def test_question_stops_working_and_followup_uses_same_session(self):
        task = self.create(bot_id='designer')
        with patch('beam_core.requests.post', return_value=Response({'tool_calls': [
                tool('ask_user', question='Which size?', options=['Small', 'Large'])]})):
            self.crew.start_task(task['id'])
            self.finished(task['id'], 'waiting_input')
        detail = self.crew.task(task['id'])
        self.assertFalse(detail['run']['working'])
        self.assertFalse(self.crew.state(self.pid)['working'])
        with patch('beam_core.requests.post', return_value=Response({'content': 'Using Large.'})):
            self.crew.start_task(task['id'], 'Large')
            self.finished(task['id'])
        self.assertEqual(self.current(task['id'])['session_id'], task['session_id'])
        users = [e['text'] for e in self.store.events(task['session_id']) if e['type'] == 'user']
        self.assertEqual(users[-1], 'Large')

    def test_approval_is_not_working_and_denial_does_not_write(self):
        task = self.create(bot_id='engineer', mode='ask')
        def model(*args, **kwargs):
            messages = kwargs['json']['messages']
            if any(m.get('tool_name') == 'write_file' for m in messages):
                return Response({'content': 'The change was declined.'})
            return Response({'tool_calls': [tool('write_file', path='declined.txt', content='no')]})
        with patch('beam_core.requests.post', side_effect=model):
            self.crew.start_task(task['id'])
            detail = until(lambda: self.crew.task(task['id']) if self.crew.task(task['id'])['pending'] else None)
            self.assertFalse(detail['run']['working'])
            self.assertFalse((self.folder / 'declined.txt').exists())
            self.agent.approve(task['session_id'], detail['pending']['action_id'], False)
            self.finished(task['id'])
        self.assertFalse((self.folder / 'declined.txt').exists())

    def test_queued_stop_cannot_be_started_by_scheduler(self):
        held = threading.Event()
        release = threading.Event()
        def model(*args, **kwargs):
            held.set()
            release.wait(4)
            return Response({'content': 'Completed held task.'})
        first = self.create(bot_id='engineer')
        second = self.create(bot_id='writer')
        try:
            with patch('beam_core.requests.post', side_effect=model) as posted:
                self.crew.start_task(first['id'])
                self.assertTrue(held.wait(3))
                self.crew.start_task(second['id'])
                self.assertEqual(self.current(second['id'])['status'], 'queued')
                self.crew.stop_task(second['id'])
                release.set()
                self.finished(first['id'])
                time.sleep(.2)
                self.assertEqual(self.current(second['id'])['status'], 'stopped')
                self.assertEqual(posted.call_count, 1)
        finally:
            release.set()

    def test_code_run_occupies_shared_project_until_finished(self):
        code = self.store.create_session(self.pid)
        run = Run()
        self.agent.runs[code['id']] = run
        task = self.create()
        # Starting Crew is allowed to queue, but never execute while Code holds the project.
        with patch('beam_core.requests.post', return_value=Response({'content': 'Crew complete.'})) as posted:
            self.crew.start_task(task['id'])
            time.sleep(.2)
            self.assertEqual(posted.call_count, 0)
            self.assertEqual(self.current(task['id'])['status'], 'queued')
            run.status = 'done'
            self.finished(task['id'])
        self.assertEqual(posted.call_count, 1)

    def test_invalid_references_and_empty_task_are_rejected(self):
        for data in ({'bot_id': 'missing'}, {'prompt': ''}, {'mode': 'unsafe'}, {'effort': 'infinite'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.create(**data)
        with self.assertRaises(ValueError):
            self.crew.save_brief('missing-project', 'Brief')
        with self.assertRaises(ValueError):
            self.crew.start_task('missing-task')
        with self.assertRaises(ValueError):
            self.crew.save_bot({'id': 'engineer', 'name': '', 'role': 'Engineer'})


    def test_restart_marks_unfinished_work_interrupted_without_resuming(self):
        states = ('queued', 'running', 'approval', 'stopping', 'waiting_team')
        tasks = [self.create(title='Interrupted ' + state) for state in states]
        self.crew.close()
        with self.store.lock, self.store.db:
            for task, state in zip(tasks, states):
                self.store.db.execute('UPDATE crew_tasks SET status=? WHERE id=?', (state, task['id']))
        with patch('beam_core.requests.post') as posted:
            self.crew = Crew(self.store, self.agent)
            current = self.crew.state(self.pid)
            time.sleep(.1)
        self.assertTrue(all(t['status'] == 'interrupted' for t in current['tasks']))
        self.assertFalse(current['working'])
        self.assertEqual(posted.call_count, 0)
        self.assertTrue(all(not self.crew.task(t['id'])['run']['working'] for t in tasks))

    def test_plan_mode_cannot_change_durable_memory(self):
        task = self.create(bot_id='engineer', mode='plan')
        def model(*args, **kwargs):
            if any(m.get('tool_name') == 'crew_remember' for m in kwargs['json']['messages']):
                return Response({'content': 'The proposed memory change was not applied.'})
            return Response({'tool_calls': [tool('crew_remember', text='This should not be saved.')]})
        with patch('beam_core.requests.post', side_effect=model):
            self.crew.start_task(task['id'])
            self.finished(task['id'])
        engineer = next(b for b in self.crew.state(self.pid)['bots'] if b['id'] == 'engineer')
        self.assertEqual(engineer['memory'], '')
        results = [e for e in self.store.events(task['session_id']) if e['type'] == 'result']
        self.assertIn('Plan mode', results[0]['result']['error'])

    def test_specialist_cannot_delegate_even_if_model_requests_it(self):
        task = self.create(bot_id='engineer', mode='edit')
        def model(*args, **kwargs):
            if any(m.get('tool_name') == 'crew_delegate' for m in kwargs['json']['messages']):
                return Response({'content': 'Finishing my own task.'})
            return Response({'tool_calls': [tool('crew_delegate', bot_id='writer', title='Unrequested child', prompt='Do another task.')]})
        with patch('beam_core.requests.post', side_effect=model):
            self.crew.start_task(task['id'])
            self.finished(task['id'])
        self.assertEqual(len(self.crew.state(self.pid)['tasks']), 1)
        results = [e for e in self.store.events(task['session_id']) if e['type'] == 'result']
        self.assertIn('Unknown tool', results[0]['result']['error'])

    def test_archiving_member_preserves_history_and_prevents_new_work(self):
        original = self.create(bot_id='writer')
        self.crew.save_bot({'id': 'writer', 'archived': True})
        self.assertEqual(self.current(original['id'])['bot_id'], 'writer')
        with self.assertRaises(ValueError):
            self.create(bot_id='writer')
        with self.assertRaises(ValueError):
            self.crew.start_task(original['id'])
        with self.assertRaises(ValueError):
            self.crew.save_bot({'id': 'lead', 'archived': True})

    def test_missing_key_does_not_create_a_ghost_active_task(self):
        before = len(self.crew.state(self.pid)['tasks'])
        with patch.object(self.store, 'key', return_value=''):
            with self.assertRaises(ValueError):
                self.crew.create_task(self.pid, {'prompt': 'Start without a key', 'start': True})
        self.assertEqual(len(self.crew.state(self.pid)['tasks']), before)
        self.assertFalse(self.crew.state(self.pid)['working'])

    def test_delegation_is_bounded_and_children_inherit_permissions(self):
        root = self.create(mode='plan', effort='low')
        run = Run()
        with self.crew.lock:
            for n in range(12):
                result = self.crew._execute(root['id'], root['session_id'], run, 'plan', 'crew_delegate',
                                            {'bot_id': 'engineer', 'title': 'Part ' + str(n), 'prompt': 'Inspect one part.'})
                self.assertEqual(result['status'], 'queued')
            with self.assertRaises(ValueError):
                self.crew._execute(root['id'], root['session_id'], run, 'plan', 'crew_delegate',
                                   {'bot_id': 'writer', 'title': 'Excess child', 'prompt': 'Keep expanding.'})
            children = [t for t in self.crew.state(self.pid)['tasks'] if t['parent_id'] == root['id']]
            self.assertEqual(len(children), 12)
            self.assertTrue(all(t['mode'] == 'plan' and t['effort'] == 'low' for t in children))
            self.crew.stop_task(root['id'])
        self.assertTrue(all(t['status'] == 'stopped' for t in self.crew.state(self.pid)['tasks']))
        self.assertFalse(list(self.folder.iterdir()))

    def test_blocked_stream_close_cannot_stall_task_stop_or_app_close(self):
        parent = self.create()
        other = self.create(title='Another active stream')
        first, second = Run(), Run()
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]

        def blocked_stop(index):
            entered[index].set()
            release.wait(5)
            finished[index].set()

        try:
            with patch.object(first, 'stop', side_effect=lambda: blocked_stop(0)) as stop_first, \
                    patch.object(second, 'stop', side_effect=lambda: blocked_stop(1)) as stop_second:
                # Hold the scheduler while constructing an in-flight stream and a queued handoff.
                with self.crew.lock:
                    child = self.crew._create(self.pid, 'engineer', 'Queued handoff', 'Inspect files',
                                              'ask', 'high', queued=True, parent_id=parent['id'])
                    self.agent.runs[parent['session_id']] = first
                    self.crew._owned.add(parent['session_id'])
                    self.crew._update(parent['id'], status='running')
                    started = time.monotonic()
                    stopped = self.crew.stop_task(parent['id'])
                    stop_seconds = time.monotonic() - started
                self.assertLess(stop_seconds, 1, 'Closing a stalled stream blocked task cancellation')
                self.assertEqual(stopped['status'], 'stopping')
                self.assertTrue(first.cancelled.is_set())
                self.assertTrue(first.decision.is_set())
                self.assertTrue(entered[0].wait(1))
                self.assertFalse(release.is_set())
                self.assertEqual(self.current(child['id'])['status'], 'stopped')

                # A second owned stream exercises close independently of the already-stopped run.
                with self.crew.lock:
                    self.agent.runs[other['session_id']] = second
                    self.crew._owned.add(other['session_id'])
                    self.crew._update(other['id'], status='running')
                started = time.monotonic()
                self.crew.close()
                close_seconds = time.monotonic() - started
                self.assertLess(close_seconds, 1, 'Closing a stalled stream blocked app shutdown')
                self.assertTrue(second.cancelled.is_set())
                self.assertTrue(second.decision.is_set())
                self.assertTrue(entered[1].wait(1))
                self.assertEqual(stop_first.call_count, 1, 'Repeated cancellation must not start duplicate stream closes')
                self.assertEqual(stop_second.call_count, 1)
        finally:
            release.set()
            for index, event in enumerate(entered):
                if event.is_set():
                    finished[index].wait(2)
            first.status = second.status = 'stopped'

class CrewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        folder = self.root / 'project'
        folder.mkdir()
        self.app = create_app(self.root / 'data', 'crew-fixture-token')
        self.client = self.app.test_client()
        self.headers = {'X-Beam-Token': 'crew-fixture-token'}
        self.app.store.save_settings({'reflection':'off'})
        self.pid = self.app.store.add_project(folder)['id']

    def tearDown(self):
        self.app.crew.close()
        self.app.agent.integrations.close()
        self.app.agent.servers.close()
        self.app.store.db.close()
        self.temp.cleanup()

    def get(self, path):
        return self.client.get('/api/' + path, headers=self.headers)

    def post(self, path, body):
        return self.client.post('/api/' + path, json=body, headers=self.headers)

    def draft(self, **extra):
        response = self.post('crew/tasks', {'project_id': self.pid, 'bot_id': 'lead',
                                          'title': 'A saved task', 'prompt': 'A task to perform',
                                          'start': False, **extra})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json

    def test_crew_routes_require_local_token_and_same_origin(self):
        path = '/api/crew?project_id=' + self.pid
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(path, headers={**self.headers, 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get(path, headers=self.headers, base_url='http://evil.example').status_code, 403)
        self.assertEqual(self.get('crew?project_id=' + self.pid).status_code, 200)

    def test_code_state_excludes_crew_sessions_but_retains_code_history(self):
        code = self.app.store.create_session(self.pid)
        draft = self.draft()
        ids = {s['id'] for s in self.get('state').json['sessions']}
        self.assertIn(code['id'], ids)
        self.assertNotIn(draft['session_id'], ids)
        detail = self.get('crew/tasks/' + draft['id'])
        self.assertEqual(detail.status_code, 200, detail.get_data(as_text=True))
        self.assertEqual(detail.json['task']['id'], draft['id'])

    def test_crew_detail_never_exposes_private_messages_or_key(self):
        draft = self.draft()
        self.app.store.save_messages(draft['session_id'], [{'role': 'assistant', 'thinking': 'private-thoughts-fixture', 'content': 'Visible answer'}])
        with patch.object(self.app.store, 'key', return_value='private-key-fixture'):
            detail = self.get('crew/tasks/' + draft['id'])
            state = self.get('crew?project_id=' + self.pid)
        self.assertEqual(detail.status_code, 200)
        for response in (detail, state):
            self.assertNotIn(b'private-thoughts-fixture', response.data)
            self.assertNotIn(b'private-key-fixture', response.data)

    def test_generic_code_run_cannot_bypass_crew_context_and_queue(self):
        draft = self.draft()
        with patch.object(self.app.store, 'key', return_value='fixture-key'), patch('beam_core.requests.post') as posted:
            response = self.post('sessions/' + draft['session_id'] + '/run', {'prompt': 'Bypass Crew', 'mode': 'edit'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(posted.call_count, 0)
        self.assertEqual(self.get('crew/tasks/' + draft['id']).json['task']['status'], 'draft')

    def test_invalid_bodies_and_references_are_json_errors(self):
        for body in ([], None, 'text'):
            with self.subTest(body=body):
                response = self.post('crew/tasks', body)
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.json)
        self.assertEqual(self.get('crew?project_id=missing').status_code, 400)
        self.assertEqual(self.get('crew/tasks/missing').status_code, 400)
        self.assertEqual(self.post('crew/tasks/missing/start', {}).status_code, 400)
        self.assertEqual(self.post('crew/tasks/missing/stop', {}).status_code, 400)


    def test_legacy_history_import_restores_crew_schema_and_code_sessions(self):
        old = Store(self.root / 'legacy-data')
        old_pid = old.add_project(self.root / 'project')['id']
        saved = old.create_session(old_pid)
        old.emit(saved['id'], 'assistant', text='A previous Code answer.')
        old.db.close()
        bridge = NativeBridge(self.app)
        bridge._window = SimpleNamespace(create_file_dialog=lambda *args, **kwargs: [str(self.root / 'legacy-data')])
        fake_webview = SimpleNamespace(FileDialog=SimpleNamespace(FOLDER='folder'))
        with patch.dict('sys.modules', {'webview': fake_webview}):
            result = bridge.import_history()
        self.assertTrue(result['ok'])
        state = self.get('state')
        self.assertIn(saved['id'], {s['id'] for s in state.json['sessions']})
        crew = self.get('crew?project_id=' + old_pid)
        self.assertEqual(crew.status_code, 200, crew.get_data(as_text=True))
        self.assertEqual(len(crew.json['bots']), 6)
        self.assertEqual(crew.json['tasks'], [])
        computer=self.get('computer?project_id='+old_pid)
        self.assertEqual(computer.status_code,200)
        self.assertIsNone(computer.json['target'])
        self.assertEqual(self.post('computer/target',{'project_id':old_pid,'target':'local'}).status_code,200)
        self.assertTrue(list(self.app.store.root.glob('before-import-*.sqlite3')))

if __name__ == '__main__':
    unittest.main()
