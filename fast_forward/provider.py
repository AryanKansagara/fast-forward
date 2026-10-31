"""Generation-only Cerebras boundary. Candidate text is never imported or run here."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = 'https://api.cerebras.ai/v1/chat/completions'
MAX_RESPONSE_BYTES = 262_144
MAX_SOURCE_BYTES = 65_536
MAX_TASK_BYTES = 32_768
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z')
SCHEMA = {
    'type': 'object',
    'properties': {'path': {'type': 'string', 'enum': ['solution.py']},
                   'source': {'type': 'string'}},
    'required': ['path', 'source'], 'additionalProperties': False,
}
STATUSES = {'accepted', 'api_error', 'transport_error', 'oversized_response',
            'invalid_response', 'invalid_usage', 'refused', 'truncated',
            'invalid_candidate', 'credential_echo'}


class ProviderConfigurationError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def https_transport(payload, headers, timeout):
    """Fixed HTTPS origin; bounded read; no redirects or implicit retries."""
    request = Request(ENDPOINT, data=json.dumps(payload).encode(),
                      headers=headers, method='POST')
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        # Upstream bodies/headers can contain echoed credentials or private prompts.
        return error.code, b''


def valid_usage(value, completion_cap=None):
    if not isinstance(value, dict):
        return None
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if any(type(value.get(k)) is not int or value[k] < 0 for k in keys):
        return None
    if value['prompt_tokens'] + value['completion_tokens'] != value['total_tokens']:
        return None
    if completion_cap is not None and value['completion_tokens'] > completion_cap:
        return None
    return {k: value[k] for k in keys}


def valid_candidate(value):
    try:
        return (isinstance(value, dict) and set(value) == {'path', 'source'}
            and value['path'] == 'solution.py' and isinstance(value['source'], str)
            and bool(value['source'])
            and len(value['source'].encode()) <= MAX_SOURCE_BYTES
                and '\x00' not in value['source'])
    except UnicodeEncodeError:
        return False


class CerebrasProvider:
    """Generate a quarantined record; transport injection is marked mock, never live."""
    def __init__(self, *, model, max_completion_tokens, timeout=30.0, transport=None):
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise ProviderConfigurationError('An explicit valid model identifier is required')
        if type(max_completion_tokens) is not int or not 1 <= max_completion_tokens <= 8192:
            raise ProviderConfigurationError('Completion cap must be an integer from 1 to 8192')
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ProviderConfigurationError('Timeout must be finite and between 0 and 60 seconds')
        self.model, self.cap, self.timeout = model, max_completion_tokens, timeout
        self.transport = transport or https_transport
        self.transport_kind = 'mock_contract' if transport is not None else 'live_https'

    def generate(self, task):
        try:
            task_bytes = task.encode('utf-8') if isinstance(task, str) else b''
        except UnicodeEncodeError:
            task_bytes = b''
        if not task_bytes or len(task_bytes) > MAX_TASK_BYTES:
            raise ProviderConfigurationError('Task must contain 1 to 32768 UTF-8 bytes')
        key = os.environ.get('CEREBRAS_API_KEY', '')
        if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ProviderConfigurationError('Set a valid CEREBRAS_API_KEY environment variable')
        if key in task or key in self.model:
            raise ProviderConfigurationError('Credential material must not be included in task/model')
        payload = {
            'model': self.model, 'max_completion_tokens': self.cap,
            'stream': False, 'n': 1,
            'messages': [
                {'role': 'system', 'content': 'Return a replacement for solution.py as JSON. '
                 'Only path and source are allowed. Do not request tool execution.'},
                {'role': 'user', 'content': task}],
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'candidate_source', 'strict': True, 'schema': SCHEMA}},
        }
        record = {
            'schema_version': 1, 'provider': 'cerebras',
            'transport_kind': self.transport_kind, 'requested_model': self.model,
            'reported_model': None, 'task_sha256': hashlib.sha256(task.encode()).hexdigest(),
            'completion_cap': self.cap, 'status': 'transport_error',
            'error_code': 'transport_error', 'candidate': None,
            'usage': None, 'usage_kind': 'unknown',
            'generation_elapsed_seconds': 0.0, 'execution': 'not_executed',
        }
        start = time.monotonic()
        try:
            status, raw = self.transport(payload, {
                'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, self.timeout)
        except Exception:
            # Never stringify a transport exception: requests and keys may be embedded.
            return self._finish(record, start, 'transport_error')
        if type(status) is not int or not 200 <= status < 300:
            return self._finish(record, start, 'api_error')
        if not isinstance(raw, bytes):
            return self._finish(record, start, 'invalid_response')
        if len(raw) > MAX_RESPONSE_BYTES:
            return self._finish(record, start, 'oversized_response')
        if key.encode() in raw:
            return self._finish(record, start, 'credential_echo')
        try:
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self._finish(record, start, 'invalid_response')
        if 'usage' in response and response['usage'] is not None:
            usage = valid_usage(response['usage'], self.cap)
            if usage is None:
                return self._finish(record, start, 'invalid_usage')
            record['usage'] = usage
            record['usage_kind'] = ('provider_reported' if self.transport_kind == 'live_https'
                                    else 'mock_reported')
        model = response.get('model')
        if isinstance(model, str) and key in model:
            return self._finish(record, start, 'credential_echo')
        if isinstance(model, str) and MODEL.fullmatch(model):
            record['reported_model'] = model
        choices = response.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return self._finish(record, start, 'invalid_response')
        choice = choices[0]
        message = choice.get('message')
        if not isinstance(message, dict):
            return self._finish(record, start, 'invalid_response')
        if message.get('refusal') or choice.get('finish_reason') == 'content_filter':
            return self._finish(record, start, 'refused')
        if choice.get('finish_reason') == 'length':
            return self._finish(record, start, 'truncated')
        if choice.get('finish_reason') != 'stop' or message.get('tool_calls'):
            return self._finish(record, start, 'invalid_response')
        try:
            candidate = json.loads(message['content'])
        except (KeyError, ValueError, TypeError, RecursionError):
            return self._finish(record, start, 'invalid_candidate')
        if not valid_candidate(candidate):
            return self._finish(record, start, 'invalid_candidate')
        if key in candidate['source']:
            return self._finish(record, start, 'credential_echo')
        record['candidate'] = candidate
        return self._finish(record, start, 'accepted')

    @staticmethod
    def _finish(record, start, status):
        record['generation_elapsed_seconds'] = round(time.monotonic() - start, 6)
        record['status'] = status
        record['error_code'] = None if status == 'accepted' else status
        return record
