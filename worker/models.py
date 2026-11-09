"""Model catalog, effort profiles and reflection settings for Beam.

Ollama does not publish a stable per-model free/paid table and retires cloud
models regularly, so this module never treats its own list as authoritative:
the live ``/api/tags`` list from the user's key wins, and the curated entries
below only add labels, usage bands and ordering to whatever is really there.
"""
from __future__ import annotations
import re
import threading
import time

import requests

DEFAULT_MODEL = 'beam'           # original Reflection Beam alias, always kept
AUTO_MODEL = 'auto'
TAGS_URL = 'https://ollama.com/api/tags'
_VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$')

# usage: 1 light · 2 medium · 3 heavy · 4 extra heavy (Ollama's own relative bands).
# Free plans run one cloud model at a time with light usage, so lighter bands last longer.
CURATED = [
    {'id': 'beam', 'label': 'Beam (default)', 'note': 'Reflection Beam, the original Beam model', 'usage': 2, 'think': 'bool', 'pick': 'default'},
    {'id': 'gpt-oss:120b', 'label': 'GPT-OSS 120B', 'note': 'Strong reasoning and tool use; best all-round free-friendly pick', 'usage': 2, 'think': 'levels', 'pick': 'free'},
    {'id': 'gpt-oss:20b', 'label': 'GPT-OSS 20B', 'note': 'Fastest and lightest; ideal for quick chats and reviewers', 'usage': 1, 'think': 'levels', 'pick': 'free'},
    {'id': 'qwen3.5:397b', 'label': 'Qwen 3.5 397B', 'note': 'Large general and coding model', 'usage': 2, 'think': 'bool', 'pick': 'free'},
    {'id': 'deepseek-v4-flash', 'label': 'DeepSeek V4 Flash', 'note': 'Very long context, strong reasoning and code', 'usage': 2, 'think': 'bool', 'pick': 'free'},
    {'id': 'minimax-m2.7', 'label': 'MiniMax M2.7', 'note': 'Agentic coding and tool use', 'usage': 2, 'think': 'bool', 'pick': 'free'},
    {'id': 'nemotron-3-super', 'label': 'Nemotron 3 Super', 'note': 'Multi-agent reasoning', 'usage': 2, 'think': 'bool', 'pick': 'free'},
    {'id': 'gemma4:31b', 'label': 'Gemma 4 31B', 'note': 'Compact, vision-capable generalist', 'usage': 2, 'think': 'bool', 'pick': 'free'},
    {'id': 'glm-5.2', 'label': 'GLM 5.2', 'note': 'Frontier coding and long documents', 'usage': 3, 'think': 'bool', 'pick': 'heavy'},
    {'id': 'kimi-k2.6', 'label': 'Kimi K2.6', 'note': 'Long-horizon agentic coding', 'usage': 3, 'think': 'bool', 'pick': 'heavy'},
    {'id': 'mistral-large-3:675b', 'label': 'Mistral Large 3', 'note': 'Large multilingual generalist', 'usage': 3, 'think': 'bool', 'pick': 'heavy'},
    {'id': 'deepseek-v4-pro', 'label': 'DeepSeek V4 Pro', 'note': 'Deepest reasoning; consumes limits fastest', 'usage': 4, 'think': 'bool', 'pick': 'heavy'},
]
_BY_ID = {m['id']: m for m in CURATED}

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
        if model.endswith(suffix):
            model = model[:-len(suffix)]
    if not _VALID.match(model):
        raise ValueError('That model name is not valid')
    return model


def meta(model):
    if model == AUTO_MODEL:
        return {'id': model, 'label': 'Auto', 'note': 'Choose an available Ollama Cloud model for this task and effort', 'pick': 'default'}
    return _BY_ID.get(model) or {'id': model, 'label': model, 'note': '', 'usage': 2, 'think': 'bool', 'pick': 'other'}


def route(prompt, effort, key):
    """Rank compatible models using the task, effort and the account's live list."""
    ids = live_ids(key)
    if not ids:
        return DEFAULT_MODEL
    cad = re.search(r'\b(?:cad|3d|stl|step file|glb|mesh|mechanical assembly)\b', prompt, re.I)
    coding = re.search(r'\b(?:app|server|code|bug|fix|repo|script|build|test|implement|refactor|debug)\b', prompt, re.I)
    lightweight = ('gpt-oss:20b', 'deepseek-v4-flash')
    general = ('gpt-oss:120b', 'qwen3.5:397b', 'gemma4:31b', DEFAULT_MODEL)
    specialist = ('qwen3.5:397b', 'minimax-m2.7', 'deepseek-v4-flash') if cad or coding else ()
    premium = ('glm-5.2', 'kimi-k2.6', 'deepseek-v4-pro') if effort == 'max' else ()
    ordered = (lightweight + specialist + general) if effort == 'low' else premium + specialist + general + lightweight
    return next((name for name in ordered if name in ids), sorted(ids)[0])


def think_value(model, effort, enabled):
    """The ``think`` field Ollama expects: levels for GPT-OSS, a boolean elsewhere."""
    if not enabled:
        return 'low' if model.startswith('gpt-oss') else False   # GPT-OSS cannot fully disable reasoning
    if model.startswith('gpt-oss'):
        return EFFORT_PROFILES.get(effort, EFFORT_PROFILES['high'])['think_level']
    return True


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
    """IDs your key can see right now, or None when Ollama cannot be reached."""
    if not key:
        return None
    with _lock:
        if _cache['key'] == key and time.time() - _cache['at'] < ttl:
            return _cache['ids']
    ids = None
    try:
        response = requests.get(TAGS_URL, headers={'Authorization': 'Bearer ' + key}, timeout=(5, 10))
        if response.status_code == 200:
            ids = {clean(m.get('model') or m.get('name') or '', '') for m in response.json().get('models', []) if (m.get('model') or m.get('name'))}
            ids.discard('')
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
