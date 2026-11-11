"""Ollama Cloud model discovery and effort settings."""
from __future__ import annotations

import re
import threading
import time

import requests
from ollama_chat import DEFAULT_MODEL, MODELS_URL

AUTO_MODEL = 'auto'
_VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$')
CURATED = [
    {'id': DEFAULT_MODEL, 'label': 'Nemotron 3 Super', 'note': 'Default · coding and multi-agent reasoning', 'pick': 'default'},
    {'id': 'beam', 'label': 'Reflection Beam', 'note': 'Coding and long-running agent tasks · access depends on your plan', 'pick': 'default'},
    {'id': 'gpt-oss:120b', 'label': 'GPT-OSS 120B', 'note': 'Reasoning and tool use', 'pick': 'other'},
    {'id': 'gpt-oss:20b', 'label': 'GPT-OSS 20B', 'note': 'Lighter reasoning and tool use', 'pick': 'other'},
]
_BY_ID = {entry['id']: entry for entry in CURATED}
_LEGACY = {'reflection/beam-free': 'beam', 'reflection/beam': 'beam',
           'nvidia/nemotron-3-super-120b-a12b': DEFAULT_MODEL,
           'openai/gpt-oss-120b': 'gpt-oss:120b', 'openai/gpt-oss-20b': 'gpt-oss:20b'}

EFFORT_PROFILES = {
    'low': {'reflect': 0, 'think_level': 'low'},
    'medium': {'reflect': 0, 'think_level': 'medium'},
    'high': {'reflect': 1, 'think_level': 'high'},
    'max': {'reflect': 2, 'think_level': 'high'},
}


def clean(model, fallback=DEFAULT_MODEL):
    if not model:
        return fallback
    model = str(model).strip()
    for suffix in ('-cloud', ':cloud'):
        if model.endswith(suffix):
            model = model[:-len(suffix)]
            break
    if not _VALID.fullmatch(model):
        raise ValueError('That model name is not valid')
    return _LEGACY.get(model, model)


def meta(model):
    if model == AUTO_MODEL:
        return {'id': model, 'label': 'Auto', 'note': 'Choose a model from the Ollama Cloud catalog', 'pick': 'default'}
    return _BY_ID.get(model) or {'id': model, 'label': model, 'note': '', 'pick': 'other'}


def route(effort, key):
    live = live_models(key)
    order = ('gpt-oss:20b', DEFAULT_MODEL, 'beam') if effort == 'low' else (DEFAULT_MODEL, 'beam', 'gpt-oss:120b', 'gpt-oss:20b')
    return next((name for name in order if live and name in live), DEFAULT_MODEL)


def think_value(model, effort, enabled):
    model = clean(model)
    if model.startswith('gpt-oss:'):
        return EFFORT_PROFILES.get(effort, EFFORT_PROFILES['high'])['think_level'] if enabled else 'low'
    if model in {DEFAULT_MODEL, 'beam'}:
        return bool(enabled)
    return None


def reflect_rounds(effort, settings=None):
    rounds = EFFORT_PROFILES.get(effort, EFFORT_PROFILES['high'])['reflect']
    setting = (settings or {}).get('reflection', 'auto')
    if setting == 'off':
        return 0
    if setting == 'always':
        return max(rounds, 1)
    return rounds


_cache = {'at': 0.0, 'key': '', 'models': None}
_lock = threading.Lock()


def live_models(key, ttl=600):
    if not key:
        return None
    with _lock:
        if _cache['key'] == key and time.monotonic() - _cache['at'] < ttl:
            return _cache['models']
    models = None
    try:
        response = requests.get(MODELS_URL, headers={'Authorization': 'Bearer ' + key}, timeout=(5, 10), allow_redirects=False)
        if response.status_code == 200:
            models = {}
            for item in response.json().get('models', []):
                try:
                    ident = clean(item.get('model') or item.get('name'), '')
                except ValueError:
                    continue
                if ident:
                    models[ident] = dict(meta(ident))
    except (requests.RequestException, ValueError):
        models = None
    with _lock:
        _cache.update(at=time.monotonic(), key=key, models=models)
    return models


def catalog(key=None, selected=None):
    live = live_models(key)
    entries = [meta(AUTO_MODEL)] if selected == AUTO_MODEL else []
    for item in CURATED:
        if item['id'] in {DEFAULT_MODEL, 'beam'} or live is None or item['id'] in live:
            entries.append(dict(item))
    known = {entry['id'] for entry in entries}
    if live:
        entries.extend(live[ident] for ident in sorted(live.keys() - known))
    if selected and selected not in {entry['id'] for entry in entries}:
        entries.append(dict(meta(selected)))
    return {'models': entries, 'live': live is not None}
