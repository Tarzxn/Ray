import json
import unittest
from unittest.mock import Mock, patch

from groq_chat import CHAT_URL, DEFAULT_MODEL, post_chat, request_payload, stream_packets


def sse(event):
    return b'data: ' + json.dumps(event).encode()


class GroqChatTests(unittest.TestCase):
    def test_request_replays_saved_tool_results_with_matching_ids(self):
        payload = request_payload({
            'model': DEFAULT_MODEL, 'stream': True, 'think': 'high', 'options': {'num_predict': 2048},
            'messages': [
                {'role': 'user', 'content': 'Read the page'},
                {'role': 'assistant', 'content': '', 'thinking': 'private reasoning',
                 'tool_calls': [{'function': {'name': 'browser_snapshot', 'arguments': {}}}]},
                {'role': 'tool', 'tool_name': 'browser_snapshot', 'content': 'Page content'},
            ],
        })
        assistant, result = payload['messages'][1:]
        call = assistant['tool_calls'][0]
        self.assertEqual(result['tool_call_id'], call['id'])
        self.assertEqual(call['function']['arguments'], '{}')
        self.assertNotIn('thinking', assistant)
        self.assertEqual(payload['reasoning_effort'], 'high')
        self.assertEqual(payload['max_completion_tokens'], 2048)

    def test_stream_reassembles_call_fragments_and_reports_usage(self):
        response = Mock()
        response.iter_lines.return_value = [
            sse({'choices': [{'delta': {'content': 'Opening the page.'}}]}),
            sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_abc',
                 'function': {'name': 'browser_open', 'arguments': '{"url":'}}]}}]}),
            sse({'choices': [{'delta': {'tool_calls': [{'index': 0,
                 'function': {'arguments': '"https://example.com"}'}}]}, 'finish_reason': 'tool_calls'}],
                 'x_groq': {'usage': {'prompt_tokens': 50, 'completion_tokens': 12}}}),
            b'data: [DONE]',
        ]
        text, final = list(stream_packets(response))
        self.assertEqual(text['message']['content'], 'Opening the page.')
        call = final['message']['tool_calls'][0]
        self.assertEqual(call['id'], 'call_abc')
        self.assertEqual(json.loads(call['function']['arguments']), {'url': 'https://example.com'})
        self.assertEqual((final['prompt_eval_count'], final['eval_count']), (50, 12))

    def test_disconnect_does_not_release_partial_tool_calls(self):
        response = Mock()
        response.iter_lines.return_value = [
            sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_abc',
                 'function': {'name': 'browser_open', 'arguments': '{"url":'}}]}}]}),
        ]
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            list(stream_packets(response))

    def test_nonstreaming_review_uses_groq_endpoint_and_normalizes_response(self):
        with patch('groq_chat.requests.post') as post:
            response = post.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {'choices': [{'message': {'content': 'OK'}}],
                                          'usage': {'prompt_tokens': 9, 'completion_tokens': 2}}
            with post_chat(CHAT_URL, json={'model': DEFAULT_MODEL, 'stream': False,
                          'messages': [{'role': 'user', 'content': 'Review'}],
                          'options': {'num_predict': 128}}, timeout=(15, 120)) as reply:
                self.assertEqual(reply.json()['message']['content'], 'OK')
        self.assertEqual(post.call_args.args[0], 'https://api.groq.com/openai/v1/chat/completions')
        self.assertFalse(post.call_args.kwargs['json']['stream'])
