"""Groq model catalog, effort profiles and reflection settings for Ray."""
from __future__ import annotations
import re
import threading
import time

import requests
from groq_chat import DEFAULT_MODEL, MODELS_URL, REASONING_MODELS

AUTO_MODEL = 'auto'
_VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$')

CURATED = [
    {'id': DEFAULT_MODEL, 'label': 'GPT-OSS 120B', 'note': 'General reasoning, coding and tool use', 'pick': 'default'},
    {'id': 'openai/gpt-oss-20b', 'label': 'GPT-OSS 20B', 'note': 'Lighter reasoning and tool use', 'pick': 'free'},
    {'id': 'qwen/qwen3.8-27b', 'label': 'Qwen 3.8 27B', 'note': 'Coding and tool use', 'pick': 'free'},
]
_BY_ID = {m['id']: m for m in CURATED}
_LEGACY = {'beam': DEFAULT_MODEL, 'nemotron-3-super': DEFAULT_MODEL,
           'gpt-oss:120b': DEFAULT_MODEL, 'gpt-oss:20b': 'openai/gpt-oss-20b'}

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
        return {'id': model, 'label': 'Auto', 'note': 'Choose an available Groq tool-capable model', 'pick': 'default'}
    return _BY_ID.get(model) or {'id': model, 'label': model, 'note': '', 'pick': 'other'}


def route(effort, key):
    """Choose a known tool-capable model from the account's live list."""
    ids = live_ids(key)
    light = 'openai/gpt-oss-20b'
    order = (DEFAULT_MODEL, 'qwen/qwen3.8-27b', light)
    if effort == 'low':
        order = (light,) + order[:-1]
    return next((name for name in order if ids and name in ids), DEFAULT_MODEL)


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


_cache = {'at': 0.0, 'key': '', 'ids': None}
_lock = threading.Lock()


def live_ids(key, ttl=600):
    """IDs your key can see right now, or None when Groq cannot be reached."""
    if not key:
        return None
    with _lock:
        if _cache['key'] == key and time.time() - _cache['at'] < ttl:
            return _cache['ids']
    ids = None
    try:
        response = requests.get(MODELS_URL, headers={'Authorization': 'Bearer ' + key}, timeout=(5, 10), allow_redirects=False)
        if response.status_code == 200:
            ids = {m['id'] for m in response.json().get('data', [])
                   if m.get('active', True) and _VALID.fullmatch(m.get('id') or '')}
    except (requests.RequestException, ValueError):
        ids = None
    with _lock:
        _cache.update(at=time.time(), key=key, ids=ids)
    return ids


def catalog(key=None, selected=None):
    """Picker entries: curated models that exist for this key, plus any other live models."""
    ids = live_ids(key)
    entries = [meta(AUTO_MODEL)] if selected == AUTO_MODEL else []
    for item in CURATED:
        available = ids is None or item['id'] in ids or item['id'] == DEFAULT_MODEL
        if available:
            entries.append(dict(item))
    if ids:
        known = {e['id'] for e in entries}
        for extra in sorted(ids - known):
            entries.append(dict(meta(extra), pick='other'))
    if selected and selected not in {e['id'] for e in entries}:
        entries.append(dict(meta(selected), pick='other'))
    return {'models': entries, 'live': ids is not None}
