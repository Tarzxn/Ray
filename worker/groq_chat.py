"""Translate Ray's saved conversations to and from Groq chat completions."""
import json
from collections import deque
from contextlib import contextmanager

import requests

CHAT_URL = 'https://api.groq.com/openai/v1/chat/completions'
MODELS_URL = 'https://api.groq.com/openai/v1/models'
DEFAULT_MODEL = 'openai/gpt-oss-120b'
REASONING_MODELS = {DEFAULT_MODEL, 'openai/gpt-oss-20b', 'qwen/qwen3.8-27b'}


def request_payload(payload):
    messages = []
    pending = deque()
    for index, message in enumerate(payload['messages']):
        role = message['role']
        content = message.get('content') or ''
        if role == 'tool' and len(content) > 8000:
            content = content[:8000] + '\n[Tool result shortened for Groq. Read a smaller section if needed.]'
        row = {'role': role, 'content': content}
        if role == 'tool':
            if not pending:
                continue
            row['tool_call_id'] = pending.popleft()
        else:
            # Old or interrupted sessions can lack some tool results.
            while pending:
                messages.append({'role': 'tool', 'tool_call_id': pending.popleft(),
                                 'content': 'Tool execution was interrupted.'})
        if role == 'assistant' and message.get('tool_calls'):
            row['tool_calls'] = []
            for offset, call in enumerate(message['tool_calls']):
                call_id = call.get('id') or f'call_history_{index}_{offset}'
                function = call['function']
                arguments = function.get('arguments', {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                row['tool_calls'].append({'id': call_id, 'type': 'function',
                                          'function': {'name': function['name'], 'arguments': arguments}})
                pending.append(call_id)
        messages.append(row)
    while pending:
        messages.append({'role': 'tool', 'tool_call_id': pending.popleft(),
                         'content': 'Tool execution was interrupted.'})

    model = payload['model']
    result = {'model': model, 'messages': messages, 'stream': payload['stream'],
              'max_completion_tokens': payload['options']['num_predict']}
    if payload.get('tools'):
        result['tools'] = payload['tools']
    if model in REASONING_MODELS:
        effort = payload.get('think')
        result['reasoning_effort'] = effort if effort in ('low', 'medium', 'high') else ('medium' if effort else 'low')
    return result


def stream_packets(response):
    """Yield Ray's text, usage and complete tool-call packets from Groq SSE."""
    calls = {}
    usage = {}
    finished = False
    for line in response.iter_lines():
        if not line.startswith(b'data:'):
            continue
        data = line[5:].strip()
        if data == b'[DONE]':
            finished = True
            break
        event = json.loads(data)
        if event.get('error'):
            raise ValueError('Groq interrupted the response. Retry the request.')
        usage = event.get('usage') or (event.get('x_groq') or {}).get('usage') or usage
        for choice in event.get('choices', []):
            delta = choice.get('delta') or {}
            if delta.get('content'):
                yield {'message': {'content': delta['content']}}
            for fragment in delta.get('tool_calls') or []:
                call = calls.setdefault(fragment['index'], {'id': '', 'function': {'name': '', 'arguments': ''}})
                if fragment.get('id'):
                    call['id'] = fragment['id']
                function = fragment.get('function') or {}
                call['function']['name'] += function.get('name') or ''
                call['function']['arguments'] += function.get('arguments') or ''
            if choice.get('finish_reason') == 'length' and calls:
                raise ValueError('Groq reached the output limit while generating a tool call.')
            finished = finished or bool(choice.get('finish_reason'))
    if not finished:
        raise ValueError('Groq disconnected before finishing the response.')
    if any(not call['function']['name'] for call in calls.values()):
        raise ValueError('Groq returned an incomplete tool call.')
    yield {'message': {'tool_calls': [calls[index] for index in sorted(calls)]}, 'done': True,
           'prompt_eval_count': usage.get('prompt_tokens', 0), 'eval_count': usage.get('completion_tokens', 0)}


def completion_packet(event):
    usage = event.get('usage') or {}
    return {'message': event['choices'][0]['message'], 'prompt_eval_count': usage.get('prompt_tokens', 0),
            'eval_count': usage.get('completion_tokens', 0)}


class ChatResponse:
    def __init__(self, response):
        self.response = response
        self.status_code = response.status_code

    def close(self):
        self.response.close()

    def packets(self):
        return stream_packets(self.response)

    def json(self):
        return completion_packet(self.response.json())


@contextmanager
def post_chat(url, *, json, **kwargs):
    with requests.post(url, json=request_payload(json), **kwargs) as response:
        yield ChatResponse(response)
