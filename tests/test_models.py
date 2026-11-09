"""Model picker, effort-aware thinking, self-review and Crew scheduling. No live requests."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import models
from beam_core import Agent, Run, Store
from crew import Crew


class Reply:
    status_code = 200

    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_lines(self):
        yield json.dumps({'message': self.message, 'done': True}).encode()

    def json(self):
        return {'message': self.message}


class CatalogTests(unittest.TestCase):
    def test_clean_strips_cloud_suffix_and_rejects_junk(self):
        self.assertEqual(models.clean('gpt-oss:120b-cloud'), 'gpt-oss:120b')
        self.assertEqual(models.clean('qwen3.5:cloud'), 'qwen3.5')
        self.assertEqual(models.clean(''), 'beam')
        for bad in ('../etc', 'a b', 'x;rm', '-lead', 'a' * 90):
            with self.assertRaises(ValueError):
                models.clean(bad)

    def test_think_levels_for_gpt_oss_and_booleans_elsewhere(self):
        self.assertEqual(models.think_value('gpt-oss:120b', 'max', True), 'high')
        self.assertEqual(models.think_value('gpt-oss:120b', 'low', True), 'low')
        self.assertEqual(models.think_value('gpt-oss:20b', 'low', False), 'low')   # cannot be fully disabled
        self.assertIs(models.think_value('qwen3.5', 'high', True), True)
        self.assertIs(models.think_value('qwen3.5', 'low', False), False)

    def test_reflection_rounds_follow_effort_and_setting(self):
        self.assertEqual([models.reflect_rounds(e) for e in ('low', 'medium', 'high', 'max')], [0, 0, 1, 2])
        self.assertEqual(models.reflect_rounds('max', {'reflection': 'off'}), 0)
        self.assertEqual(models.reflect_rounds('low', {'reflection': 'always'}), 1)

    def test_catalog_filters_to_live_models_and_keeps_default(self):
        with patch.object(models, 'live_ids', return_value={'gpt-oss:120b', 'brand-new-model'}):
            ids = [m['id'] for m in models.catalog('key')['models']]
        self.assertIn('beam', ids)
        self.assertIn('gpt-oss:120b', ids)
        self.assertIn('brand-new-model', ids)       # new cloud models appear without a code change
        self.assertNotIn('deepseek-v4-pro', ids)    # not visible to this key

    def test_catalog_falls_back_to_curated_when_offline(self):
        with patch.object(models, 'live_ids', return_value=None):
            result = models.catalog('key')
        self.assertFalse(result['live'])
        self.assertGreaterEqual(len(result['models']), 8)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        folder = root / 'project'
        folder.mkdir()
        self.store = Store(root / 'data')
        self.agent = Agent(self.store)
        self.pid = self.store.add_project(folder)['id']
        self.sid = self.store.create_session(self.pid)['id']
        patch.object(self.store, 'key', return_value='fixture-key').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.agent.integrations.close)

    def wait(self):
        run = self.agent.runs[self.sid]
        run.worker.join(10)
        self.assertFalse(run.worker.is_alive())
        return run

    def test_selected_model_and_thinking_reach_ollama(self):
        with patch('beam_core.requests.post', return_value=Reply({'content': 'Hi there.'})) as posted:
            self.agent.start(self.sid, 'hello', 'plan', 'high', model='gpt-oss:120b-cloud')
            run = self.wait()
        payload = posted.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'gpt-oss:120b')
        self.assertEqual(payload['think'], 'high')
        self.assertEqual(run.model, 'gpt-oss:120b')

    def test_saved_default_model_is_used_when_none_is_sent(self):
        self.store.save_settings({'model': 'deepseek-v4-flash'})
        with patch('beam_core.requests.post', return_value=Reply({'content': 'ok'})) as posted:
            self.agent.start(self.sid, 'hello', 'plan', 'low')
            self.wait()
        self.assertEqual(posted.call_args.kwargs['json']['model'], 'deepseek-v4-flash')

    def test_invalid_model_is_rejected_before_any_request(self):
        with patch('beam_core.requests.post') as posted:
            with self.assertRaises(ValueError):
                self.agent.start(self.sid, 'hello', 'plan', 'low', model='bad model;')
        posted.assert_not_called()

    def test_review_that_finds_problems_triggers_a_corrected_answer(self):
        long_draft = 'Here is my analysis. ' * 30   # long enough to deserve a review
        replies = [Reply({'content': long_draft}),
                   Reply({'content': json.dumps({'verdict': 'revise', 'issues': ['The summary never states the total.']})}),
                   Reply({'content': 'Corrected answer. The total is 42.'}),
                   Reply({'content': json.dumps({'verdict': 'ok'})})]
        with patch('beam_core.requests.post', side_effect=replies) as posted:
            self.agent.start(self.sid, 'Summarise and give the total', 'plan', 'max')
            run = self.wait()
        self.assertEqual(posted.call_count, 4)
        self.assertEqual(run.reflections, 2)
        kinds = [e for e in self.store.events(self.sid) if e.get('type') == 'reflect']
        self.assertEqual([e['state'] for e in kinds if e['state'] != 'start'], ['revise', 'ok'])
        final = [e for e in self.store.events(self.sid) if e.get('type') == 'assistant'][-1]
        self.assertIn('42', final['text'])
        # the reviewer call is non-streaming and has no tools
        review = posted.call_args_list[1].kwargs['json']
        self.assertFalse(review['stream'])
        self.assertNotIn('tools', review)

    def test_review_failure_never_breaks_the_run(self):
        long_draft = 'Detailed answer. ' * 40
        replies = [Reply({'content': long_draft}), RuntimeError('network down')]
        with patch('beam_core.requests.post', side_effect=replies):
            self.agent.start(self.sid, 'Explain', 'plan', 'high')
            run = self.wait()
        self.assertEqual(run.status, 'done')

    def test_short_chat_answers_skip_review(self):
        with patch('beam_core.requests.post', return_value=Reply({'content': 'Sure.'})) as posted:
            self.agent.start(self.sid, 'hi', 'plan', 'max')
            self.wait()
        self.assertEqual(posted.call_count, 1)

    def test_reflection_can_be_turned_off(self):
        self.store.save_settings({'reflection': 'off'})
        with patch('beam_core.requests.post', return_value=Reply({'content': 'Long. ' * 200})) as posted:
            self.agent.start(self.sid, 'explain', 'plan', 'max')
            self.wait()
        self.assertEqual(posted.call_count, 1)

    def test_settings_validate_parallelism(self):
        with self.assertRaises(ValueError):
            self.store.save_settings({'crew_parallel': 9})
        self.store.save_settings({'crew_parallel': 2, 'reflection': 'always', 'model': 'gpt-oss:20b'})
        saved = self.store.settings()
        self.assertEqual((saved['crew_parallel'], saved['reflection'], saved['model']), (2, 'always', 'gpt-oss:20b'))

    def _run_tools(self, mode, calls):
        replies = [Reply({'tool_calls': calls}), Reply({'content': 'Finished.'})]
        with patch('beam_core.requests.post', side_effect=replies):
            self.agent.start(self.sid, 'do it', mode, 'low')
            return self.wait()

    def test_auto_mode_runs_edits_and_commands_without_any_approval(self):
        calls = [{'function': {'name': 'write_file', 'arguments': {'path': 'a.txt', 'content': 'hi'}}},
                 {'function': {'name': 'run_command', 'arguments': {'command': 'echo auto-ok'}}}]
        with patch.object(self.agent, 'request_approval', side_effect=AssertionError('asked')) as asked:
            run = self._run_tools('auto', calls)
        asked.assert_not_called()
        self.assertEqual(run.status, 'done')
        events = self.store.events(self.sid)
        self.assertTrue(any('auto-ok' in json.dumps(e) for e in events if e.get('type') == 'result'))
        self.assertEqual(next(Path(self.temp.name).glob('project/a.txt')).read_text(), 'hi')

    def test_edit_mode_still_asks_before_commands(self):
        calls = [{'function': {'name': 'run_command', 'arguments': {'command': 'echo hi'}}}]
        with patch.object(self.agent, 'request_approval', return_value=False) as asked:
            self._run_tools('edit', calls)
        asked.assert_called_once()

    def test_auto_mode_keeps_hard_safety_blocks(self):
        calls = [{'function': {'name': 'run_command', 'arguments': {'command': 'pkill python'}}}]
        run = self._run_tools('auto', calls)
        results = [e for e in self.store.events(self.sid) if e.get('type') == 'result']
        self.assertIn('terminate unrelated processes', json.dumps(results))

    def test_plan_mode_still_blocks_writes(self):
        calls = [{'function': {'name': 'write_file', 'arguments': {'path': 'b.txt', 'content': 'x'}}}]
        self._run_tools('plan', calls)
        self.assertFalse((Path(self.temp.name) / 'project' / 'b.txt').exists())


class CrewModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        folder = root / 'project'
        folder.mkdir()
        self.store = Store(root / 'data')
        self.store.save_settings({'reflection': 'off'})
        self.agent = Agent(self.store)
        self.pid = self.store.add_project(folder)['id']
        self.crew = Crew(self.store, self.agent)
        patch.object(self.store, 'key', return_value='fixture-key').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.crew.close)
        self.addCleanup(self.agent.integrations.close)
        self.addCleanup(self.temp.cleanup)

    def test_member_model_is_saved_validated_and_exposed(self):
        bot = self.crew.save_bot({'id': 'engineer', 'model': 'glm-5.2:cloud'})
        self.assertEqual(bot['model'], 'glm-5.2')
        with self.assertRaises(ValueError):
            self.crew.save_bot({'id': 'engineer', 'model': 'no way'})
        self.assertEqual(self.crew.save_bot({'id': 'engineer', 'model': ''})['model'], '')
        self.assertIn('model', self.crew.state(self.pid)['bots'][0])

    def test_task_runs_with_the_members_model(self):
        self.crew.save_bot({'id': 'researcher', 'model': 'gpt-oss:120b'})
        with patch('beam_core.requests.post', return_value=Reply({'content': 'Done.'})) as posted:
            task = self.crew.create_task(self.pid, {'bot_id': 'researcher', 'title': 'Look', 'prompt': 'Find things', 'mode': 'plan', 'effort': 'low'})
            for _ in range(60):
                self.crew._tick()
                if posted.called:
                    break
                time.sleep(0.05)
        self.assertEqual(posted.call_args.kwargs['json']['model'], 'gpt-oss:120b')

    def _scheduler(self):
        """Replace Agent.start with a fake that registers a live run, so only the scheduler is tested."""
        started = []

        def fake_start(session, prompt, mode, effort, **kwargs):
            started.append(mode)
            self.agent.runs[session] = Run()

        patch.object(self.agent, 'start', side_effect=fake_start).start()
        return started

    def _queue(self, count, mode):
        for n in range(count):
            self.crew.create_task(self.pid, {'bot_id': 'researcher', 'title': f'T{n}', 'prompt': 'go', 'mode': mode, 'effort': 'low'})

    def _settle(self):
        for _ in range(6):
            self.crew._tick()

    def test_read_only_tasks_share_slots_but_never_beyond_the_limit(self):
        self.store.save_settings({'crew_parallel': 2})
        started = self._scheduler()
        self._queue(3, 'plan')
        self._settle()
        self.assertEqual(started, ['plan', 'plan'])

    def test_default_is_one_task_at_a_time(self):
        started = self._scheduler()
        self._queue(2, 'plan')
        self._settle()
        self.assertEqual(len(started), 1)

    def test_editing_tasks_never_run_beside_others(self):
        self.store.save_settings({'crew_parallel': 3})
        started = self._scheduler()
        self._queue(1, 'plan')
        self._queue(2, 'edit')
        self._settle()
        self.assertEqual(started, ['plan'])

    def test_editing_task_runs_alone_when_nothing_else_is(self):
        self.store.save_settings({'crew_parallel': 3})
        started = self._scheduler()
        self._queue(2, 'edit')
        self._settle()
        self.assertEqual(started, ['edit'])


class ResearchTests(unittest.TestCase):
    def tools(self, ollama=None, free=None):
        from types import SimpleNamespace
        from web_tools import WebTools
        config = SimpleNamespace(read=lambda: {'search_provider': 'free'}, store=SimpleNamespace(key=lambda: 'k'))
        tools = WebTools(config)
        patch.object(tools, '_ollama_search', side_effect=ollama or (lambda q, n: [])).start()
        patch.object(tools, '_free_search', side_effect=free or (lambda q, n: [])).start()
        self.addCleanup(patch.stopall)
        return tools

    def test_ollama_results_are_preferred_and_free_search_is_the_fallback(self):
        hit = [{'title': 'A', 'url': 'https://a.example', 'snippet': 's'}]
        tools = self.tools(ollama=lambda q, n: hit, free=lambda q, n: self.fail('should not fall back'))
        self.assertEqual(tools.search('x')['results'], hit)
        tools = self.tools(ollama=lambda q, n: [], free=lambda q, n: hit)
        self.assertEqual(tools.search('y')['results'], hit)

    def test_research_runs_parallel_searches_and_reads_distinct_sources(self):
        def free(query, count):
            return [{'title': query + str(i), 'url': f'https://{query}.example/{i}', 'snippet': 'snip'} for i in range(3)]
        tools = self.tools(free=free)
        opened = []

        def fake_open(url):
            opened.append(url)
            if 'bad' in url:
                raise ValueError('blocked')
            return {'title': 'T', 'text': 'body ' * 3000}
        with patch.object(tools, 'open', side_effect=fake_open):
            result = tools.research(['alpha', 'beta', 'bad'], open_top=4)
        self.assertEqual(len(result['searches']), 3)
        self.assertEqual(len(set(opened)), len(opened))            # never reads one URL twice
        self.assertLessEqual(len(opened), 4)
        self.assertTrue(all(len(s.get('text', '')) <= 6000 for s in result['sources']))
        self.assertTrue(any('error' in s for s in result['sources']))   # a bad page is reported, not fatal
        self.assertIn('untrusted', result['source_note'])

    def test_research_validates_input(self):
        tools = self.tools()
        with self.assertRaises(ValueError):
            tools.research([])
        with self.assertRaises(ValueError):
            tools.research(['   '])


class ApiTests(unittest.TestCase):
    def test_models_endpoint_and_run_model_are_wired_through_the_flask_app(self):
        from desktop import create_app
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(Path(temp) / 'data', 'fixture-token')
            headers = {'X-Beam-Token': 'fixture-token'}
            client = app.test_client()
            try:
                with patch.object(app.store, 'key', return_value='k'), patch.object(models, 'live_ids', return_value={'gpt-oss:120b'}):
                    data = client.get('/api/models', headers=headers).get_json()
                    self.assertTrue(data['live'])
                    self.assertIn('gpt-oss:120b', [m['id'] for m in data['models']])
                    self.assertEqual(data['selected'], 'beam')
                    self.assertEqual(data['efforts']['max']['reflect'], 2)
                    self.assertEqual(client.post('/api/settings', headers=headers, json={'model': 'gpt-oss:120b', 'reflection': 'off', 'crew_parallel': 2}).status_code, 200)
                    again = client.get('/api/models', headers=headers).get_json()
                    self.assertEqual((again['selected'], again['reflection'], again['crew_parallel']), ('gpt-oss:120b', 'off', 2))
                    self.assertEqual(again['efforts']['max']['reflect'], 0)
                    self.assertEqual(client.get('/api/state', headers=headers).get_json()['selected_model'], 'gpt-oss:120b')
            finally:
                app.crew.close()
                app.agent.integrations.close()


if __name__ == '__main__':
    unittest.main()
