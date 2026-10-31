import json
import os
import unittest
from unittest.mock import patch

from fast_forward.provider import (CerebrasProvider, ENDPOINT, MAX_RESPONSE_BYTES,
                                   ProviderConfigurationError, https_transport)

KEY = 'contract-only-secret'


def response(source='def normalize(values):\n    return sorted(set(values))\n'):
    return {'model': 'test-model', 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'path': 'solution.py', 'source': source})}}]}


class ProviderContracts(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'CEREBRAS_API_KEY': KEY})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def generate(self, payload=None, *, status=200, raw=None):
        def transport(request, headers, timeout):
            self.assertEqual(headers['Authorization'], 'Bearer ' + KEY)
            self.assertEqual(request['model'], 'test-model')
            self.assertEqual(request['max_completion_tokens'], 64)
            schema = request['response_format']['json_schema']
            self.assertIs(schema['strict'], True)
            self.assertIs(schema['schema']['additionalProperties'], False)
            self.assertEqual(schema['schema']['properties']['path']['enum'], ['solution.py'])
            self.assertFalse(request['stream'])
            return status, raw if raw is not None else json.dumps(payload if payload is not None else response()).encode()
        return CerebrasProvider(model='test-model', max_completion_tokens=64, transport=transport).generate('Repair normalization')

    def test_strict_contract_and_mock_provenance(self):
        result = self.generate()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['usage_kind'], 'mock_reported')
        self.assertEqual(result['transport_kind'], 'mock_contract')
        self.assertEqual(result['execution'], 'not_executed')
        self.assertNotIn(KEY, json.dumps(result))

    def test_generated_source_is_data_only(self):
        source = 'raise RuntimeError("NEVER EXECUTE ME")'
        result = self.generate(response(source))
        self.assertEqual(result['candidate']['source'], source)
        self.assertEqual(result['status'], 'accepted')

    def test_refusal_and_truncation_keep_usage(self):
        for reason, status in [('length', 'truncated'), ('content_filter', 'refused')]:
            with self.subTest(reason=reason):
                payload = response()
                payload['choices'][0]['finish_reason'] = reason
                result = self.generate(payload)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['usage']['total_tokens'], 30)
                self.assertIsNone(result['candidate'])
        payload = response()
        payload['choices'][0]['message']['refusal'] = 'refused'
        self.assertEqual(self.generate(payload)['status'], 'refused')

    def test_bad_usage_rejected_and_missing_usage_unknown(self):
        for usage in [{}, {'prompt_tokens': True, 'completion_tokens': 2, 'total_tokens': 3},
                      {'prompt_tokens': -1, 'completion_tokens': 2, 'total_tokens': 1},
                      {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 99},
                      {'prompt_tokens': 1, 'completion_tokens': 65, 'total_tokens': 66}]:
            with self.subTest(usage=usage):
                payload = response()
                payload['usage'] = usage
                self.assertEqual(self.generate(payload)['status'], 'invalid_usage')
        payload = response()
        del payload['usage']
        result = self.generate(payload)
        self.assertEqual(result['status'], 'accepted')
        self.assertIsNone(result['usage'])
        self.assertEqual(result['usage_kind'], 'unknown')

    def test_response_size_invalid_json_and_secret_echo(self):
        for raw, status in [(b'x' * (MAX_RESPONSE_BYTES + 1), 'oversized_response'),
                            (b'not-json', 'invalid_response'),
                            (KEY.encode(), 'credential_echo')]:
            with self.subTest(status=status):
                result = self.generate(raw=raw)
                self.assertEqual(result['status'], status)
                self.assertNotIn(KEY, json.dumps(result))

    def test_escaped_json_credential_echo_is_removed_after_decode(self):
        payload = response(KEY)
        raw = json.dumps(payload).replace(KEY, ''.join('\\u%04x' % ord(char) for char in KEY)).encode()
        result = self.generate(raw=raw)
        self.assertEqual(result['status'], 'credential_echo')
        self.assertNotIn(KEY, json.dumps(result))

    def test_candidate_path_exact_schema_and_source_limits(self):
        candidates = [{'path': '../solution.py', 'source': 'x'},
                      {'path': 'solution.py', 'source': 'x', 'extra': 'file'},
                      {'path': 'solution.py', 'source': 'x' * 65537},
                      {'path': 'solution.py', 'source': '\ud800'},
                      {'path': 'solution.py', 'source': '\x00'}]
        for candidate in candidates:
            payload = response()
            payload['choices'][0]['message']['content'] = json.dumps(candidate)
            with self.subTest(candidate=str(candidate)[:50]):
                self.assertEqual(self.generate(payload)['status'], 'invalid_candidate')

    def test_nonterminal_choices_and_tools_rejected(self):
        for change in ['tool', 'empty', 'reason']:
            payload = response()
            if change == 'tool':
                payload['choices'][0]['message']['tool_calls'] = [{'name': 'exec'}]
            elif change == 'empty':
                payload['choices'] = []
            else:
                payload['choices'][0]['finish_reason'] = 'unknown'
            self.assertEqual(self.generate(payload)['status'], 'invalid_response')

    def test_api_and_transport_errors_do_not_leak_upstream(self):
        result = self.generate(status=429, raw=KEY.encode())
        self.assertEqual(result['status'], 'api_error')
        self.assertNotIn(KEY, json.dumps(result))
        def fail(*args):
            raise RuntimeError(KEY)
        result = CerebrasProvider(model='test-model', max_completion_tokens=64, transport=fail).generate('Repair')
        self.assertEqual(result['status'], 'transport_error')
        self.assertNotIn(KEY, json.dumps(result))

    def test_configuration_validation_before_transport(self):
        for model, cap in [('', 1), ('https://evil.invalid', 1), ('m', True), ('m', 0), ('m', 8193)]:
            with self.subTest(model=model, cap=cap), self.assertRaises(ProviderConfigurationError):
                CerebrasProvider(model=model, max_completion_tokens=cap)
        with patch.dict(os.environ, {'CEREBRAS_API_KEY': ''}), self.assertRaises(ProviderConfigurationError):
            CerebrasProvider(model='m', max_completion_tokens=1).generate('Repair')

    def test_transport_fixed_https_and_bounded_read(self):
        with patch('fast_forward.provider.build_opener') as build:
            stream = build.return_value.open.return_value.__enter__.return_value
            stream.status, stream.read.return_value = 200, b'{}'
            self.assertEqual(https_transport({}, {'Authorization': 'Bearer fake'}, 3), (200, b'{}'))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.method, 'POST')
            stream.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
            handler = build.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))

    def test_default_transport_marked_live_but_test_performs_no_network(self):
        with patch('fast_forward.provider.https_transport', return_value=(200, json.dumps(response()).encode())):
            result = CerebrasProvider(model='test-model', max_completion_tokens=64).generate('Repair')
        self.assertEqual(result['transport_kind'], 'live_https')
        self.assertEqual(result['usage_kind'], 'provider_reported')

    def test_deeply_nested_untrusted_json_is_bounded_failure(self):
        result = self.generate(raw=b'[' * 2000 + b'0' + b']' * 2000)
        self.assertEqual(result['status'], 'invalid_response')


if __name__ == '__main__':
    unittest.main()
