"""On-device privacy checkpoints before Beam sends any model payload to cloud.

Detection is deliberately transparent and best effort. Explicit [private] blocks
cover sensitive material that pattern matching cannot identify. Redaction runs
locally without inference.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

MAX_FINDINGS = 512
_DOC_NETS = tuple(ipaddress.ip_network(net) for net in (
    '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '2001:db8::/32'))
_IP4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)')
_IP6 = re.compile(r'(?<![\w])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]*(?:%[A-Za-z0-9_.-]+)?')
_EMAIL = re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+(?![\w-])')
_INTENT = re.compile(r'(?i)\b(?:my|our)\s+(?:(?:public|private|local|external|internal|wi-?fi|network)\s+){0,3}(?:ip(?:v[46])?(?:\s+address)?|email(?:\s+address)?|password|api\s+key)\b')
_SECRET_PATTERNS = tuple(re.compile(pattern, re.I if insensitive else 0) for pattern, insensitive in (
    (r'\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}', False),
    (r'\bgsk_[A-Za-z0-9_-]{20,}', False),
    (r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})', False),
    (r'\bxox[baprs]-[A-Za-z0-9-]{15,}', False),
    (r'\bAIza[A-Za-z0-9_-]{30,}', False),
    (r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', False),
    (r'\b[a-f0-9]{32}\.[A-Za-z0-9_-]{20,}\b', True),
    (r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b', False),
    (r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]+?(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|$)', False),
))
_ASSIGNMENT = re.compile(r'(?i)\b(?:[A-Za-z_]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)[A-Za-z_]*|token|authorization)[\x27\"]?\s*[:=]\s*(?:\x27([^\x27\r\n]+)\x27|\"([^\"\r\n]+)\"|([^\s,;\}\]]+))')
_BEARER = re.compile(r'(?i)\b(?:Bearer|Basic)\s+([A-Za-z0-9_./+=-]{12,})')
_BLOCK = re.compile(r'(?is)\[private\](?:.*?\[/private\]|.*\Z)|<private>(?:.*?</private>|.*\Z)')
_PRIVATE_LINE = re.compile(r'(?im)^\s*(?:private|confidential)(?:\s+data)?\s*:\s*[^\r\n]+')
_PLACEHOLDERS = {'none', 'null', 'true', 'false', 'password', 'secret', 'token', 'api_key', 'your_api_key', 'your_key', 'your_token', 'changeme', 'example', 'test', 'bearer', 'str', 'string', '...'}


class PrivacyPause(ValueError):
    """A safe, user-facing pause. No original secret or local response in errors."""


def _structural(path, key):
    if not path and key == 'model':
        return True
    if path and path[0] == 'messages' and len(path) == 2 and key in {'role', 'tool_call_id', 'tool_name'}:
        return True
    if 'tool_calls' in path and path[-1:] and isinstance(path[-1], int) and key in {'id', 'type'}:
        return True
    if path and path[-1] == 'function' and key == 'name':
        return True
    if path and path[0]=='tools' and key in {'type','required','$schema','$ref'}:
        return True
    return False


def _metadata_key(path, key):
    if not path or path[0]=='tools':
        return True
    if path[0]=='messages' and '$json' not in path:
        if len(path)==2:
            return True
        if 'tool_calls' in path and (isinstance(path[-1],int) or path[-1]=='function'):
            return True
    return False


def _secret_fields(value, path=()):
    if isinstance(value,dict):
        for key,item in value.items():
            if _structural(path,key):
                continue
            if isinstance(key,str) and isinstance(item,str) and re.fullmatch(r'(?i)(?:[A-Za-z_]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret)[A-Za-z_]*|token|authorization)',key):
                if item.strip() and item.strip().lower() not in {'none','null','...','your_api_key','your_key','your_token'} and not item.startswith(('$','<','{')):
                    yield item
            yield from _secret_fields(item,(*path,key))
    elif isinstance(value,list):
        for index,item in enumerate(value):
            yield from _secret_fields(item,(*path,index))
    elif isinstance(value,str) and value.lstrip().startswith(('{','[')):
        try:
            parsed=json.loads(value)
            if isinstance(parsed,(dict,list)):
                yield from _secret_fields(parsed,(*path,'$json'))
        except (ValueError,RecursionError):
            pass


def _strings(value: Any, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key,str) and not _metadata_key(path,key):
                yield key
            if not _structural(path, key):
                yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))
    elif isinstance(value, str):
        # Tool results and some tool-call arguments are JSON strings. Reopening
        # them prevents replacing escaped values from damaging their JSON syntax.
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    yield from _strings(parsed, (*path, '$json'))
                    return
            except (ValueError, RecursionError):
                pass
        yield value


def detect_sensitive(payload):
    """Internal findings contain values; never log or expose this return value."""
    found = {}

    def add(category, value, *, intent=False):
        if not value or len(value) > 250000:
            if value:
                raise PrivacyPause('A private block is too large to process safely. Split it into smaller sections before continuing.')
            return
        found.setdefault((category, value, intent), {'category': category, 'value': value, 'intent': intent})
        if len(found) > MAX_FINDINGS:
            raise PrivacyPause('There are too many potentially private values in this request. Use a smaller input before continuing.')

    for credential in _secret_fields(payload):
        add('Credential or API token',credential)
    for text in _strings(payload):
        for match in _BLOCK.finditer(text):
            add('Explicitly private text', match.group())
        for match in _PRIVATE_LINE.finditer(text):
            add('Explicitly private text', match.group())
        for match in _EMAIL.finditer(text):
            add('Email address', match.group())
        for pattern in (_IP4, _IP6):
            for match in pattern.finditer(text):
                value = match.group().rstrip('.')
                try:
                    try:
                        address = ipaddress.ip_address(value.split('%', 1)[0])
                    except ValueError:
                        # A single sentence colon after an IPv6 literal is not
                        # part of the address. Preserve valid :: endings first.
                        if pattern is not _IP6 or not value.endswith(':'):
                            raise
                        value=value[:-1]
                        address=ipaddress.ip_address(value.split('%',1)[0])
                    effective = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
                    if effective.is_loopback or effective.is_unspecified or any(effective.version == net.version and effective in net for net in _DOC_NETS):
                        continue
                except ValueError:
                    continue
                add('IP address', value)
        for pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                add('Credential or API token', match.group())
        for match in _BEARER.finditer(text):
            add('Credential or API token', match.group(1))
        for match in _ASSIGNMENT.finditer(text):
            value = next((part for part in match.groups() if part is not None), '')
            normalized = value.strip().lower()
            if normalized in _PLACEHOLDERS or normalized.startswith(('$', '<', '{', 'os.', 'process.', 'getenv(', 'request.', 'config.', 'settings.', 'self.')):
                continue
            # Unquoted source expressions are code, not a discovered credential.
            if match.group(3) is not None and any(char in value for char in '()[]'):
                continue
            add('Credential or API token', value)
        for match in _INTENT.finditer(text):
            add('Personal data request', match.group().lower(), intent=True)
    return list(found.values())


def _replace(value, replacements, path=()):
    if isinstance(value, dict):
        return {(key if _metadata_key(path,key) or not isinstance(key,str) else _redact_text(key,replacements)): item if _structural(path,key) else _replace(item,replacements,(*path,key)) for key,item in value.items()}
    if isinstance(value, list):
        return [_replace(item, replacements, (*path, index)) for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return json.dumps(_replace(parsed, replacements, (*path, '$json')), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    return _redact_text(value,replacements)


def _redact_text(value,replacements):
    # Longest first removes an explicit private block before smaller matches.
    # Boundaries on short passwords avoid changing innocent identifier substrings.
    for raw,replacement in sorted(replacements.items(),key=lambda item:len(item[0]),reverse=True):
        if value==raw:
            value=replacement
        elif len(raw)<8:
            value=re.sub(r'(?<![A-Za-z0-9_])'+re.escape(raw)+r'(?![A-Za-z0-9_])',lambda match:replacement,value)
        else:
            value=value.replace(raw,replacement)
    return value


class PrivacyGuard:
    def prepare(self, payload, run, checkpoint, emit=None):
        findings = detect_sensitive(payload)
        unresolved = [f for f in findings if (f['value'] not in run.privacy_intents if f['intent'] else f['value'] not in run.privacy_allowed_values and f['value'] not in run.privacy_local_values)]
        if unresolved:
            categories = sorted({f['category'] for f in unresolved})
            preview = '\n'.join(category + ': [PRIVATE CONTENT HIDDEN]' for category in categories)
            choice, _ = checkpoint(categories, preview)
            if run.cancelled.is_set() or choice == 'stop':
                run.cancelled.set()
                raise PrivacyPause('Task stopped before cloud processing.')
            actual = [f for f in unresolved if not f['intent']]
            if choice == 'local':
                for finding in actual:
                    value = finding['value']
                    if value not in run.privacy_local_values:
                        label = re.sub(r'[^A-Z0-9]+', '_', finding['category'].upper()).strip('_')
                        run.privacy_local_values[value] = f'[PRIVATE_{label}_{len(run.privacy_local_values) + 1}]'
            elif choice == 'cloud':
                run.privacy_allowed_values.update(f['value'] for f in actual)
            else:
                raise PrivacyPause('Choose how to process the private data before continuing.')
            run.privacy_intents.update(f['value'] for f in unresolved if f['intent'])
        if run.cancelled.is_set():
            raise PrivacyPause('Task stopped before cloud processing.')
        safe = _replace(payload, run.privacy_local_values)
        if run.privacy_local_values:
            safe['messages'] = [{'role': 'system', 'content': 'Privacy boundary: [PRIVATE_...] placeholders are values redacted on the worker. Their originals are unavailable to you. Do not guess, reconstruct or put invented replacements into code, commands or tool arguments. Continue coding with placeholders or ask the user when an exact value is essential. All coding remains on this cloud model.'}] + safe.get('messages', [])
        return safe
