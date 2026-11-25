"""Native Ollama Cloud transport for Ray's chat and self-review."""
import json
from contextlib import contextmanager
from pathlib import Path

import requests

CHAT_URL = 'https://ollama.com/api/chat'
MODELS_URL = 'https://ollama.com/api/tags'
DEFAULT_MODEL = 'nemotron-3-super'
LOCAL_MODEL = 'qwen2.5-coder:3b'
LOCAL_CHAT_URL = 'http://127.0.0.1:11434/api/chat'
LOCAL_FLAG = Path('/workspaces/.beam-cloud/local-inference')


def local_enabled():
    return LOCAL_FLAG.is_file()


def request_payload(payload):
    messages = []
    for message in payload['messages']:
        role = message['role']
        row = {'role': role, 'content': message.get('content') or ''}
        if role == 'tool' and message.get('tool_name'):
            row['tool_name'] = message['tool_name']
        if role == 'assistant':
            if message.get('thinking'):
                row['thinking'] = message['thinking']
            if message.get('tool_calls'):
                calls = []
                for call in message['tool_calls']:
                    function = call.get('function') or {}
                    name, arguments = function.get('name'), function.get('arguments', {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    if name:
                        calls.append({'function': {'name': name, 'arguments': arguments if isinstance(arguments, dict) else {}}})
                if calls:
                    row['tool_calls'] = calls
        messages.append(row)
    result = {**payload, 'messages': messages}
    if result.get('think') is None:
        result.pop('think', None)
    if not result.get('tools'):
        result.pop('tools', None)
    return result


def stream_packets(response):
    """Read Ollama's newline-delimited JSON until the completion marker."""
    for line in response.iter_lines():
        if not line.strip():
            continue
        packet = json.loads(line)
        if packet.get('error'):
            raise ValueError('Ollama Cloud interrupted the response. Check model access and try again.')
        yield packet
        if packet.get('done'):
            return
    raise ValueError('Ollama Cloud disconnected before finishing the response.')


class ChatResponse:
    def __init__(self, response, local=False):
        self.response = response
        self.status_code = response.status_code
        self.local = local

    def close(self):
        self.response.close()

    def packets(self):
        return stream_packets(self.response)

    def json(self):
        return self.response.json()

    def error_message(self, model):
        if self.local:
            return f'Codespace model returned HTTP {self.status_code}. Check that {LOCAL_MODEL} is installed.'
        hint = {
            401: 'Check your Ollama Cloud API key in Settings.',
            402: 'Check billing and model access on your Ollama account.',
            403: 'This account cannot access the model. Check your Ollama plan or select another model.',
            404: 'Check whether this model is still available in Ollama Cloud.',
            429: 'The usage limit was reached. Wait and try again.',
        }.get(self.status_code, 'Check the Ollama Cloud connection and retry.')
        return f'Ollama Cloud HTTP {self.status_code} for {model}. {hint}'


@contextmanager
def post_chat(url, *, json, **kwargs):
    if url != CHAT_URL:
        raise ValueError('Unsupported inference endpoint')
    if local_enabled():
        payload = request_payload(json)
        payload.update(model=LOCAL_MODEL, options={**payload.get('options', {}), 'num_ctx': 8192})
        payload.pop('think', None)
        kwargs.pop('headers', None)
        with requests.Session() as session:
            session.trust_env = False
            try:
                with session.post(LOCAL_CHAT_URL, json=payload, allow_redirects=False, **kwargs) as response:
                    yield ChatResponse(response, local=True)
            except requests.ConnectionError as error:
                raise ValueError('The Codespace model server is offline. Start it, then retry.') from error
        return
    with requests.post(CHAT_URL, json=request_payload(json), allow_redirects=False, **kwargs) as response:
        yield ChatResponse(response)
