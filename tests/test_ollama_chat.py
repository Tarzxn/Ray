import json
import unittest
from unittest.mock import Mock, patch

from ollama_chat import CHAT_URL, DEFAULT_MODEL, ChatResponse, post_chat, request_payload, stream_packets


class OllamaChatTests(unittest.TestCase):
    def test_history_converts_older_tool_call_arguments(self):
        payload = request_payload({
            'model': DEFAULT_MODEL, 'stream': True, 'think': True, 'options': {'num_predict': 2048},
            'messages': [
                {'role': 'user', 'content': 'Read the page'},
                {'role': 'assistant', 'content': '', 'thinking': 'reasoning',
                 'tool_calls': [{'id': 'old', 'type': 'function',
                                 'function': {'name': 'browser_snapshot', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_name': 'browser_snapshot', 'content': 'Page content'},
            ],
        })
        assistant, result = payload['messages'][1:]
        self.assertEqual(assistant['tool_calls'], [{'function': {'name': 'browser_snapshot', 'arguments': {}}}])
        self.assertEqual(result['tool_name'], 'browser_snapshot')
        self.assertIs(payload['think'], True)
        self.assertEqual(payload['options']['num_predict'], 2048)

    def test_stream_uses_ollama_ndjson_and_retains_usage(self):
        packets = [
            {'message': {'content': 'Opening the page.'}},
            {'message': {'tool_calls': [{'function': {'name': 'browser_open', 'arguments': {'url': 'https://example.com'}}}]}},
            {'done': True, 'prompt_eval_count': 50, 'eval_count': 12},
        ]
        response = Mock()
        response.iter_lines.return_value = [json.dumps(packet).encode() for packet in packets]
        self.assertEqual(list(stream_packets(response)), packets)

    def test_disconnect_before_done_raises(self):
        response = Mock()
        response.iter_lines.return_value = [b'{"message":{"content":"Partial"}}']
        with self.assertRaisesRegex(ValueError, 'disconnected'):
            list(stream_packets(response))

    def test_nonstreaming_review_uses_ollama_endpoint(self):
        with patch('ollama_chat.requests.post') as post:
            response = post.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {'message': {'content': 'OK'}, 'done': True}
            with post_chat(CHAT_URL, json={'model': DEFAULT_MODEL, 'stream': False,
                          'messages': [{'role': 'user', 'content': 'Review'}],
                          'options': {'num_predict': 128}}, timeout=(15, 120)) as reply:
                self.assertEqual(reply.json()['message']['content'], 'OK')
        self.assertEqual(post.call_args.args[0], 'https://ollama.com/api/chat')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertFalse(post.call_args.kwargs['json']['stream'])

    def test_other_endpoint_is_rejected_before_a_request(self):
        with patch('ollama_chat.requests.post') as post:
            with self.assertRaisesRegex(ValueError, 'Unsupported inference endpoint'):
                with post_chat('https://example.com/chat', json={}):
                    self.fail('Unexpected response')
            post.assert_not_called()

    def test_access_error_identifies_provider_status_and_model(self):
        message = ChatResponse(Mock(status_code=403)).error_message('beam')
        self.assertIn('Ollama Cloud HTTP 403 for beam', message)
