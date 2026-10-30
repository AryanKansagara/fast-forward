"""Controlled patch races. Worktrees isolate files, not hostile code."""
from __future__ import annotations
import concurrent.futures as futures
import dataclasses
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


@dataclasses.dataclass(frozen=True)
class Candidate:
    name: str
    source: str
    delay: float
    tokens: int = 100
    path: str = 'solution.py'


class Budget:
    """Reserve the known synthetic maximum before launch; never refund cancels."""
    def __init__(self, limit: int, microdollars_per_token: int = 2, cost_limit: int | None = None):
        if limit < 0 or microdollars_per_token < 0 or (cost_limit is not None and cost_limit < 0):
            raise ValueError('budget and rate must be nonnegative')
        self.limit = limit
        self.rate = microdollars_per_token
        self.cost_limit = cost_limit
        self.reserved = 0
        self.lock = threading.Lock()

    def reserve(self, tokens: int) -> bool:
        with self.lock:
            if tokens < 0 or self.reserved + tokens > self.limit:
                return False
            if self.cost_limit is not None and (self.reserved + tokens) * self.rate > self.cost_limit:
                return False
            self.reserved += tokens
            return True


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.STDOUT).strip()


def create_fixture(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Fast Forward Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'solution.py').write_text('def normalize(values):\n    return values\n')
    git(repo, 'add', 'solution.py')
    git(repo, 'commit', '-qm', 'Pinned repair fixture')
    return git(repo, 'rev-parse', 'HEAD')


FIXTURES = [
    Candidate('plausible-wrong', 'def normalize(values):\n    return sorted(values)\n', .08),
    Candidate('slow-valid', 'def normalize(values):\n    return sorted(set(values))\n', .40),
    Candidate('fast-valid', 'def normalize(values):\n    return list(sorted(set(values)))\n', .12),
]

# This verifier is outside the candidate worktree and cannot be edited by patches.
# It is not a sandbox: importing arbitrary Python would execute it with host rights.
VERIFIER = '''import importlib.util, sys
p = sys.argv[1]
s = importlib.util.spec_from_file_location("candidate_solution", p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
for x, expected in [([], []), ([3, 1, 3], [1, 3]), ([-1, 0, -1], [-1, 0]), ([2, 2], [2])]:
    assert m.normalize(x) == expected, repr(x)
'''


def run_process(command: list[str], stop: threading.Event, timeout: float) -> tuple[str, int | None]:
    """Kill the entire POSIX process group on cancellation or deadline."""
    if stop.is_set():
        return 'cancelled', None
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command, stdout=output, stderr=output, start_new_session=True, env={
            'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
        })
        start = time.monotonic()
        reason = None
        try:
            while proc.poll() is None:
                if stop.is_set():
                    reason = 'cancelled'
                    break
                if time.monotonic() - start >= timeout:
                    reason = 'timeout'
                    break
                stop.wait(.005)
        finally:
            # The parent may have exited successfully while leaving descendants.
            # Kill its original group on every outcome, then reap our direct child.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        return (reason or ('passed' if proc.returncode == 0 else 'failed'), proc.returncode)



def race(repo: Path, candidates: list[Candidate], mode='race', concurrency=2,
         token_budget=1000, deadline=5.0, verifier_timeout=1.0, cost_budget_microdollars=None) -> dict:
    if mode not in {'serial', 'race'} or concurrency < 1 or deadline <= 0:
        raise ValueError('invalid scheduler configuration')
    if any(c.tokens < 0 or c.delay < 0 for c in candidates):
        raise ValueError('candidate delay and tokens must be nonnegative')
    base = git(repo, 'rev-parse', 'HEAD')
    budget = Budget(token_budget, cost_limit=cost_budget_microdollars)
    stop = threading.Event()
    start = time.monotonic()
    records, events = [], []
    event_lock = threading.Lock()
    winner = None
    width = 1 if mode == 'serial' else concurrency

    def emit(kind, **fields):
        with event_lock:
            events.append({'at_seconds': round(time.monotonic()-start, 6), 'kind': kind, **fields})

    with tempfile.TemporaryDirectory(prefix='fast-forward-') as temporary:
        root = Path(temporary)
        verifier = root / 'verifier.py'
        verifier.write_text(VERIFIER)

        def work(index, candidate):
            path = root / f'candidate-{index}'
            registered = False
            status = 'error'
            error = None
            patch_diff = None
            try:
                emit('started', candidate=candidate.name)
                if candidate.path != 'solution.py':
                    status = 'rejected-path'
                    return {'name': candidate.name, 'status': status}
                # Git metadata mutations are serialized to avoid worktree-lock races.
                with event_lock:
                    git(repo, 'worktree', 'add', '--detach', '-q', str(path), base)
                registered = True
                if stop.wait(candidate.delay):
                    status = 'cancelled'
                else:
                    (path / candidate.path).write_text(candidate.source)
                    emit('patch-applied', candidate=candidate.name)
                    remaining = deadline - (time.monotonic()-start)
                    if remaining <= 0:
                        status = 'timeout'
                    else:
                        status, _ = run_process([sys.executable, '-I', str(verifier), str(path/'solution.py')], stop,
                                                min(verifier_timeout, remaining))
                    if status == 'passed':
                        patch_diff = git(path, 'diff', '--', 'solution.py')
                        emit('verified', candidate=candidate.name)
            except Exception as exc:
                error = type(exc).__name__ + ': ' + str(exc)
                status = 'error'
            finally:
                if registered:
                    with event_lock:
                        git(repo, 'worktree', 'remove', '--force', str(path))
                emit('finished', candidate=candidate.name, status=status)
            result = {'name': candidate.name, 'status': status}
            if patch_diff:
                result['patch_diff'] = patch_diff
            if error:
                result['error'] = error
            return result

        pending = {}
        next_index = 0
        with futures.ThreadPoolExecutor(max_workers=width) as pool:
            while True:
                while not stop.is_set() and len(pending) < width and next_index < len(candidates):
                    if time.monotonic() - start >= deadline:
                        stop.set()
                        break
                    c = candidates[next_index]
                    if not budget.reserve(c.tokens):
                        records.append({'name': c.name, 'status': 'budget-rejected'})
                        next_index += 1
                        continue
                    emit('reserved', candidate=c.name, synthetic_tokens=c.tokens)
                    pending[pool.submit(work, next_index, c)] = next_index
                    next_index += 1
                if not pending:
                    break
                remaining = deadline-(time.monotonic()-start)
                if remaining <= 0:
                    stop.set()
                done, _ = futures.wait(pending, timeout=max(0, min(.01, remaining)), return_when=futures.FIRST_COMPLETED)
                if stop.is_set() and not done:
                    done, _ = futures.wait(pending, timeout=.05, return_when=futures.FIRST_COMPLETED)
                for future in done:
                    pending.pop(future)
                    result = future.result()
                    records.append(result)
                    if result['status'] == 'passed' and winner is None:
                        winner = result['name']
                        stop.set()
                        emit('winner', candidate=winner)
                if not pending and (stop.is_set() or next_index >= len(candidates)):
                    break
        # Executor completion ensures worker cleanup finished before temp dir deletion.
    return {
        'schema_version': 1, 'provider': 'deterministic-fixture', 'inference_is_synthetic': True,
        'mode': mode, 'base_commit': base, 'winner': winner,
        'elapsed_seconds': round(time.monotonic()-start, 6),
        'synthetic_reserved_tokens': budget.reserved,
        'synthetic_estimated_microdollars': budget.reserved * budget.rate,
        'budget_policy': 'All launched candidates charged full synthetic reservation, including cancelled work',
        'results': records, 'events': events,
        'worktrees_remaining': len(git(repo, 'worktree', 'list', '--porcelain').split('worktree '))-1,
    }
