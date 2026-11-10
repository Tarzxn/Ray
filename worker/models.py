"""Vercel AI Gateway model catalog, effort profiles and reflection settings for Ray."""
from __future__ import annotations
import re
import threading
import time

import requests
from gateway_chat import DEFAULT_MODEL, MODELS_URL, REASONING_MODELS

AUTO_MODEL = 'auto'
_VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$')

CURATED = [
    {'id': DEFAULT_MODEL, 'label': 'Beam 1.0 Free', 'note': 'Default · coding and agents · limited-time free model', 'pick': 'free'},
    {'id': 'nvidia/nemotron-3.5-lightning-free', 'label': 'Nemotron 3.5 Lightning Free', 'note': 'Fast reasoning and tool use · free variant', 'pick': 'free'},
    {'id': 'nvidia/nemotron-3-super-120b-a12b', 'label': 'Nemotron 3 Super', 'note': 'Multi-agent reasoning', 'pick': 'value'},
    {'id': 'openai/gpt-oss-120b', 'label': 'GPT-OSS 120B', 'note': 'Open-weight reasoning and tool use', 'pick': 'value'},
    {'id': 'deepseek/deepseek-v4.1-flash-1017', 'label': 'DeepSeek V4.1 Flash', 'note': 'Coding and long-context tasks', 'pick': 'value'},
    {'id': 'anthropic/claude-sonnet-5.5', 'label': 'Claude Sonnet 5.5', 'note': 'Coding and everyday agent work', 'pick': 'premium'},
    {'id': 'anthropic/claude-opus-5.5', 'label': 'Claude Opus 5.5', 'note': 'Complex engineering work', 'pick': 'premium'},
    {'id': 'anthropic/claude-fable-5.2', 'label': 'Claude Fable 5.2', 'note': 'Open-ended engineering and orchestration', 'pick': 'premium'},
    {'id': 'openai/gpt-5.6-sol', 'label': 'GPT-5.6 Sol', 'note': 'Complex coding and reasoning', 'pick': 'premium'},
    {'id': 'google/gemini-4-argon', 'label': 'Gemini 4 Argon', 'note': 'Multimodal reasoning', 'pick': 'premium'},
]
_BY_ID = {m['id']: m for m in CURATED}
_LEGACY = {'beam': DEFAULT_MODEL, 'nemotron-3-super': 'nvidia/nemotron-3-super-120b-a12b',
           'gpt-oss:120b': 'openai/gpt-oss-120b', 'gpt-oss:20b': 'openai/gpt-oss-20b'}

# Each effort level controls depth of reasoning, step/time budgets and how many
# self-review rounds run after the agent believes it is finished.
EFFORT_PROFILES = {
    'low':    {'reflect': 0, 'think_level': 'low'},
    'medium': {'reflect': 0, 'think_level': 'medium'},
    'high':   {'reflect': 1, 'think_level': 'high'},
    'max':    {'reflect': 2, 'think_level': 'high'},
}


def clean(model, fallback=DEFAULT_MODEL):
    """Return a safe model ID. Unknown IDs are allowed (the live list changes)."""
    if not model:
        return fallback
    model = str(model).strip()
    for suffix in ('-cloud', ':cloud'):
        if '/' not in model and model.endswith(suffix):
            model = model[:-len(suffix)]
    if not _VALID.fullmatch(model):
        raise ValueError('That model name is not valid')
    return _LEGACY.get(model, model)


def meta(model):
    if model == AUTO_MODEL:
        return {'id': model, 'label': 'Auto (free models)', 'note': 'Choose a known free tool-capable model', 'pick': 'default'}
    return _BY_ID.get(model) or {'id': model, 'label': model, 'note': '', 'pick': 'other'}


def route(effort, key):
    """Choose a known tool-capable model from the account's live list."""
    live = live_models(key)
    light = 'nvidia/nemotron-3.5-lightning-free'
    order = (DEFAULT_MODEL, light)
    if effort == 'low':
        order = (light, DEFAULT_MODEL)
    return next((name for name in order if live and name in live), DEFAULT_MODEL)


def think_value(model, effort, enabled):
    if clean(model) not in REASONING_MODELS:
        return None
    return EFFORT_PROFILES.get(effort, EFFORT_PROFILES['high'])['think_level'] if enabled else 'low'


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
    """List the Gateway's language models that advertise tool support."""
    if not key:
        return None
    with _lock:
        if _cache['key'] == key and time.time() - _cache['at'] < ttl:
            return _cache['models']
    models = None
    try:
        response = requests.get(MODELS_URL, headers={'Authorization': 'Bearer ' + key}, timeout=(5, 10), allow_redirects=False)
        if response.status_code == 200:
            models = {}
            for item in response.json().get('data', []):
                ident = item.get('id') or ''
                if (not _VALID.fullmatch(ident) or item.get('type') != 'language' or
                        'tool-use' not in (item.get('tags') or [])):
                    continue
                entry = dict(meta(ident))
                entry['label'] = item.get('name') or entry['label']
                rates = item.get('pricing') or {}
                try:
                    cost = [float(rates[name]) * 1_000_000 for name in ('input', 'output')]
                    entry['price'] = 'Free' if cost == [0, 0] else f'${cost[0]:g} in / ${cost[1]:g} out per 1M tokens'
                    if cost == [0, 0]:
                        entry['pick'] = 'free'
                except (KeyError, TypeError, ValueError):
                    pass
                models[ident] = entry
    except (requests.RequestException, ValueError):
        models = None
    with _lock:
        _cache.update(at=time.time(), key=key, models=models)
    return models


def catalog(key=None, selected=None):
    """Picker entries: curated models that exist for this key, plus any other live models."""
    live = live_models(key)
    entries = [meta(AUTO_MODEL)] if selected == AUTO_MODEL else []
    for item in CURATED:
        available = live is None or item['id'] in live or item['id'] == DEFAULT_MODEL
        if available:
            entries.append(live.get(item['id'], item) if live is not None else dict(item))
    if live:
        known = {e['id'] for e in entries}
        for extra in sorted(live.keys() - known):
            entries.append(live[extra])
    if selected and selected not in {e['id'] for e in entries}:
        entries.append(dict(meta(selected), pick='other'))
    return {'models': entries, 'live': live is not None}
