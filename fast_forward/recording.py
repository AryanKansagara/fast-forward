"""Safe recording inspection and paired validation of known, authored source only."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile

from .core import Candidate, FIXTURES, create_fixture, race
from .provider import MODEL, STATUSES, valid_candidate, valid_usage

MAX_RECORDING_BYTES = 2_000_000
# Imported recordings are bounded evidence, not arbitrary numeric containers.
# One day is well above this adapter's 60-second request timeout. Together with
# the 100-record limit it also bounds every possible timing aggregate.
MAX_GENERATION_SECONDS = 86_400
TRUSTED_SOURCES = frozenset(c.source for c in FIXTURES)


class RecordingError(ValueError):
    pass


def read_recording(path):
    path = Path(path)
    with path.open('rb') as stream:
        raw = stream.read(MAX_RECORDING_BYTES + 1)
    if len(raw) > MAX_RECORDING_BYTES:
        raise RecordingError('Recording exceeds 2 MB')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RecordingError('Recording must be valid UTF-8 JSON') from None
    if isinstance(payload, dict) and 'records' in payload:
        payload = payload['records']
    elif isinstance(payload, dict):
        payload = [payload]
    return validate_records(payload)


def validate_records(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise RecordingError('Expected 1 to 100 generation records')
    for record in records:
        if not isinstance(record, dict) or type(record.get('schema_version')) is not int or record['schema_version'] != 1:
            raise RecordingError('Unsupported generation-record schema')
        if not isinstance(record.get('provider'), str) or record['provider'] not in {'cerebras', 'deterministic-fixture'}:
            raise RecordingError('Unsupported provider')
        origin = record.get('transport_kind')
        allowed = {'mock_contract', 'live_https'} if record['provider'] == 'cerebras' else {'fixture'}
        if not isinstance(origin, str) or origin not in allowed or record.get('execution') != 'not_executed':
            raise RecordingError('Invalid record origin or execution state')
        cap = None
        if record['provider'] == 'cerebras':
            model, cap = record.get('requested_model'), record.get('completion_cap')
            if not isinstance(model, str) or not MODEL.fullmatch(model):
                raise RecordingError('Missing or invalid requested model')
            if type(cap) is not int or not 1 <= cap <= 8192:
                raise RecordingError('Missing or invalid completion cap')
        if not isinstance(record.get('status'), str) or record['status'] not in STATUSES:
            raise RecordingError('Invalid record status')
        elapsed = record.get('generation_elapsed_seconds')
        # Compare bounds before converting integers to float: isfinite(10**400)
        # raises OverflowError instead of rejecting an untrusted record cleanly.
        if type(elapsed) not in {int, float} or not 0 <= elapsed <= MAX_GENERATION_SECONDS:
            raise RecordingError('Invalid generation timing')
        usage = record.get('usage')
        expected_kind = {'live_https': 'provider_reported', 'mock_contract': 'mock_reported',
                         'fixture': 'synthetic'}[origin]
        if usage is None:
            if record.get('usage_kind') != 'unknown':
                raise RecordingError('Missing usage must be labeled unknown')
        elif valid_usage(usage, cap) is None or record.get('usage_kind') != expected_kind:
            raise RecordingError('Invalid usage or misleading usage provenance')
        if record['status'] == 'accepted':
            if not valid_candidate(record.get('candidate')):
                raise RecordingError('Invalid accepted candidate')
        elif record.get('candidate') is not None:
            raise RecordingError('Failed generation must not supply an executable candidate')
    return records


def fixture_recording():
    return [{
        'schema_version': 1, 'provider': 'deterministic-fixture',
        'transport_kind': 'fixture', 'requested_model': None, 'reported_model': None,
        'status': 'accepted', 'candidate': {'path': c.path, 'source': c.source},
        'usage': {'prompt_tokens': 50, 'completion_tokens': 50, 'total_tokens': c.tokens},
        'usage_kind': 'synthetic', 'generation_elapsed_seconds': c.delay,
        'execution': 'not_executed',
    } for c in FIXTURES]


def view_recording(records):
    """No candidate import, compilation, subprocess, network, or worktree operations."""
    validate_records(records)
    groups = {}
    timings = {}
    for record in records:
        origin = record['transport_kind']
        timing = timings.setdefault(origin, {'records': 0, 'seconds_sum': 0,
            'semantics': 'scripted synthetic delay' if origin == 'fixture' else 'recorded prior client wall time'})
        timing['records'] += 1
        total = timing['seconds_sum'] + record['generation_elapsed_seconds']
        if not math.isfinite(total):
            raise RecordingError('Invalid generation timing aggregate')
        timing['seconds_sum'] = round(total, 6)
        kind = record['usage_kind']
        group = groups.setdefault(kind, {'records': 0, 'prompt_tokens': 0,
                                       'completion_tokens': 0, 'total_tokens': 0})
        group['records'] += 1
        if record['usage'] is not None:
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                group[key] += record['usage'][key]
        else:
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                group[key] = None
    return {
        'schema_version': 1, 'record_count': len(records),
        'accepted_candidates': sum(r['status'] == 'accepted' for r in records),
        'usage_by_provenance': groups,
        'unknown_usage_records': sum(r['usage'] is None for r in records),
        'prior_generation_timing_by_origin': timings,
        'generation_timing_semantics': 'Prior timings grouped by origin; synthetic scripted delays are not client measurements. Sums are not parallel critical paths or newly measured end-to-end times.',
        'origin_claims': sorted({r['provider'] + ':' + r['transport_kind'] for r in records}),
        'provenance_warning': 'Recording metadata is self-reported, not independently authenticated; mock/synthetic usage is not live inference evidence',
        'candidate_execution': 'none', 'cost': 'not_estimated_without_versioned_pricing',
        'usage_coverage': 'All supplied records, including failed generations and losing candidates; unknown usage is never treated as zero',
    }


def paired_replay(records, *, concurrency=3):
    """Execute only exact built-in authored source, never arbitrary generated patches."""
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise RecordingError('Validation concurrency must be an integer from 1 to 16')
    summary = view_recording(records)
    accepted = [(i, r) for i, r in enumerate(records) if r['status'] == 'accepted']
    if not accepted:
        raise RecordingError('No accepted candidate to validate')
    if any(r['candidate']['source'] not in TRUSTED_SOURCES for _, r in accepted):
        raise RecordingError('Execution refused: recording contains source outside the authored fixture allowlist; use recorded-view')
    candidates = [Candidate(f'record-{i}', r['candidate']['source'], 0, tokens=0)
                  for i, r in accepted]
    bundle = json.dumps([r['candidate'] for _, r in accepted], sort_keys=True).encode()
    digest = hashlib.sha256(bundle).hexdigest()
    runs = []
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / 'fixture'
        base = create_fixture(repo)
        for mode in ('serial', 'race'):
            measured = race(repo, candidates, mode=mode, concurrency=concurrency)
            measured['validation_elapsed_seconds'] = measured.pop('elapsed_seconds')
            for key in ('provider', 'inference_is_synthetic', 'synthetic_reserved_tokens',
                        'synthetic_estimated_microdollars', 'budget_policy'):
                measured.pop(key, None)
            for event in measured['events']:
                if event['kind'] == 'reserved':
                    event['kind'] = 'validation-admitted'
                event.pop('synthetic_tokens', None)
            measured['candidate_bundle_sha256'] = digest
            measured['candidate_generation_executed'] = False
            runs.append(measured)
    return {
        'schema_version': 1, 'phase': 'paired_recorded_candidate_validation',
        'candidate_bundle_sha256': digest, 'base_commit': base,
        'generation_accounting_once': summary, 'runs': runs,
        'comparison_scope': 'Identical prerecorded authored patches and verifier; generation delays are not replayed. Validation timings include worktree setup and cleanup.',
        'end_to_end_inference_speedup': 'not_measured',
    }
