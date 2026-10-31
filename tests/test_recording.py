import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fast_forward.__main__ import main
from fast_forward.recording import (RecordingError, fixture_recording, paired_replay,
                                    read_recording, validate_records, view_recording,
                                    MAX_GENERATION_SECONDS)


class RecordingContracts(unittest.TestCase):
    def test_all_branches_and_failed_generation_accounted_once(self):
        records = fixture_recording()
        failed = copy.deepcopy(records[0])
        failed.update(status='truncated', candidate=None, provider='cerebras',
                      transport_kind='live_https', usage_kind='provider_reported',
                      requested_model='test-model', completion_cap=64)
        unknown = copy.deepcopy(failed)
        unknown.update(usage=None, usage_kind='unknown')
        records.extend([failed, unknown])
        summary = view_recording(records)
        self.assertEqual(summary['usage_by_provenance']['synthetic']['total_tokens'], 300)
        self.assertEqual(summary['usage_by_provenance']['provider_reported']['total_tokens'], 100)
        self.assertEqual(summary['unknown_usage_records'], 1)
        self.assertIsNone(summary['usage_by_provenance']['unknown']['total_tokens'])
        self.assertEqual(summary['record_count'], 5)

    def test_paired_replay_same_base_and_candidate_bundle(self):
        records = fixture_recording()
        failed = copy.deepcopy(records[0])
        failed.update(status='truncated', candidate=None)
        records.append(failed)
        result = paired_replay(records)
        serial, raced = result['runs']
        self.assertEqual(serial['candidate_bundle_sha256'], raced['candidate_bundle_sha256'])
        self.assertEqual(serial['base_commit'], raced['base_commit'])
        self.assertEqual(result['generation_accounting_once']['usage_by_provenance']['synthetic']['total_tokens'], 400)
        for run in result['runs']:
            self.assertFalse(run['candidate_generation_executed'])
            self.assertGreater(run['validation_elapsed_seconds'], 0)
            self.assertEqual(run['worktrees_remaining'], 1)
            self.assertNotIn('inference_is_synthetic', run)
            self.assertNotIn('elapsed_seconds', run)
            self.assertIsNotNone(run['winner'])
        self.assertEqual(result['end_to_end_inference_speedup'], 'not_measured')

    def test_live_metadata_distinct_from_synthetic_and_mock(self):
        records = fixture_recording()
        records[0].update(provider='cerebras', transport_kind='live_https', usage_kind='provider_reported', requested_model='m', completion_cap=64)
        records[1].update(provider='cerebras', transport_kind='mock_contract', usage_kind='mock_reported', requested_model='m', completion_cap=64)
        result = view_recording(records)
        self.assertEqual(set(result['usage_by_provenance']), {'synthetic', 'provider_reported', 'mock_reported'})
        self.assertIn('self-reported', result['provenance_warning'])
        self.assertEqual(set(result['prior_generation_timing_by_origin']), {'fixture', 'live_https', 'mock_contract'})

    def test_safe_view_never_executes_and_replay_rejects_generated_source(self):
        records = fixture_recording()
        records[0]['candidate']['source'] = 'raise RuntimeError("UNTRUSTED")'
        with patch('fast_forward.recording.race', side_effect=AssertionError('must not execute')):
            self.assertEqual(view_recording(records)['candidate_execution'], 'none')
            with self.assertRaises(RecordingError):
                paired_replay(records)

    def test_reject_misleading_provenance_and_malformed_records(self):
        for field, value in [('usage_kind', 'provider_reported'), ('schema_version', True),
                             ('generation_elapsed_seconds', float('nan')), ('execution', 'executed')]:
            records = fixture_recording()
            records[0][field] = value
            with self.subTest(field=field), self.assertRaises(RecordingError):
                validate_records(records)

    def test_timing_extremes_rejected_without_raw_numeric_exceptions(self):
        for value in [10 ** 400, 1e308, float('inf'), float('-inf'),
                      float('nan'), -1, True, MAX_GENERATION_SECONDS + 1]:
            with self.subTest(value=repr(value)):
                records = fixture_recording()
                for record in records:
                    record['generation_elapsed_seconds'] = value
                with self.assertRaises(RecordingError):
                    view_recording(records)

    def test_maximum_timing_aggregate_is_finite_standard_json(self):
        record = fixture_recording()[0]
        record['generation_elapsed_seconds'] = MAX_GENERATION_SECONDS
        summary = view_recording([copy.deepcopy(record) for _ in range(100)])
        total = summary['prior_generation_timing_by_origin']['fixture']['seconds_sum']
        self.assertEqual(total, 100 * MAX_GENERATION_SECONDS)
        json.dumps(summary, allow_nan=False)

    def test_timing_rejection_reaches_cli_as_sanitized_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recording.json'
            records = fixture_recording()
            records[0]['generation_elapsed_seconds'] = 10 ** 400
            path.write_text(json.dumps({'records': records}))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['recorded-view', str(path)]), 2)
            self.assertEqual(json.loads(stderr.getvalue()), {'error': 'Invalid generation timing'})

    def test_bounded_reader_and_safe_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recording.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['recorded-fixture', '--output', str(path)]), 0)
                self.assertEqual(main(['recorded-view', str(path)]), 0)
            self.assertEqual(len(read_recording(path)), 3)
            path.write_bytes(b'x' * 2_000_001)
            with self.assertRaises(RecordingError):
                read_recording(path)
            path.write_bytes(b'{')
            with self.assertRaises(RecordingError):
                read_recording(path)

    def test_cli_generate_persists_failure_usage_without_execution(self):
        record = fixture_recording()[0]
        record.update(status='truncated', candidate=None)
        with tempfile.TemporaryDirectory() as tmp:
            task, output = Path(tmp) / 'task.txt', Path(tmp) / 'result.json'
            task.write_text('Repair normalization')
            with patch('fast_forward.__main__.CerebrasProvider') as provider, contextlib.redirect_stdout(io.StringIO()):
                provider.return_value.generate.return_value = record
                code = main(['generate', '--model', 'explicit-model', '--max-completion-tokens', '128',
                             '--task', str(task), '--output', str(output)])
                self.assertEqual(code, 2)
                self.assertEqual(read_recording(output)[0]['usage']['total_tokens'], 100)
                provider.assert_called_once_with(model='explicit-model', max_completion_tokens=128, timeout=30)


if __name__ == '__main__':
    unittest.main()
