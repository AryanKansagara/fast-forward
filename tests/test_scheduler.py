import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from fast_forward.core import Budget, Candidate, FIXTURES, create_fixture, git, race, run_process

GOOD = 'def normalize(values):\n    return sorted(set(values))\n'

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)/'repo'
        create_fixture(self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def check_clean(self):
        listing = git(self.repo, 'worktree', 'list', '--porcelain')
        self.assertEqual(listing.count('worktree '), 1)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), '')
        self.assertIn('return values', (self.repo/'solution.py').read_text())

    def test_wrong_finishes_first_valid_wins_and_workers_cleanup(self):
        result = race(self.repo, FIXTURES, concurrency=3)
        self.assertEqual(result['winner'], 'fast-valid')
        statuses = {r['name']: r['status'] for r in result['results']}
        self.assertEqual(statuses['plausible-wrong'], 'failed')
        self.assertEqual(statuses['slow-valid'], 'cancelled')
        self.assertEqual(result['synthetic_reserved_tokens'], 300)
        self.check_clean()

    def test_serial_stops_after_verified_candidate(self):
        result = race(self.repo, FIXTURES, mode='serial')
        self.assertEqual(result['winner'], 'slow-valid')
        self.assertEqual(result['synthetic_reserved_tokens'], 200)
        self.check_clean()

    def test_budget_rejection_and_no_winner(self):
        result = race(self.repo, FIXTURES, token_budget=100, concurrency=3)
        self.assertIsNone(result['winner'])
        self.assertEqual(result['synthetic_reserved_tokens'], 100)
        self.assertEqual(sum(r['status']=='budget-rejected' for r in result['results']), 2)
        self.check_clean()

    def test_path_escape_and_test_replacement_rejected(self):
        cases = [Candidate('escape', GOOD, 0, path='../outside.py'), Candidate('test-edit', GOOD, 0, path='verifier.py')]
        result = race(self.repo, cases)
        self.assertTrue(all(r['status']=='rejected-path' for r in result['results']))
        self.check_clean()

    def test_invalid_python_fails_verification(self):
        result = race(self.repo, [Candidate('broken', 'this is invalid python !!!', 0)])
        self.assertIsNone(result['winner'])
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.check_clean()

    def test_verifier_timeout_cleans_worktree(self):
        candidate = Candidate('hang', 'import time\ntime.sleep(30)\n'+GOOD, 0)
        result = race(self.repo, [candidate], verifier_timeout=.05)
        self.assertEqual(result['results'][0]['status'], 'timeout')
        self.check_clean()

    def test_global_deadline_cancels_generation(self):
        result = race(self.repo, [Candidate('slow', GOOD, 30)], deadline=.05)
        self.assertIsNone(result['winner'])
        self.assertEqual(result['results'][0]['status'], 'cancelled')
        self.assertEqual(result['synthetic_reserved_tokens'], 100)
        self.check_clean()

    def test_process_cancellation(self):
        stop = threading.Event()
        timer = threading.Timer(.05, stop.set)
        timer.start()
        try:
            status, code = run_process([sys.executable, '-c', 'import time; time.sleep(30)'], stop, 5)
        finally:
            timer.cancel()
        self.assertEqual(status, 'cancelled')
        self.assertEqual(code, -9)

    def test_successful_verifier_descendant_is_not_left_running(self):
        marker = Path(self.temp.name)/'child-survived'
        child_script = ('import time; from pathlib import Path; time.sleep(.15); '
                        + f'Path({str(marker)!r}).write_text("survived")')
        source = ('import subprocess, sys\n'
                  + f'subprocess.Popen([sys.executable, "-c", {child_script!r}])\n' + GOOD)
        result = race(self.repo, [Candidate('spawn-child', source, 0)])
        self.assertEqual(result['winner'], 'spawn-child')
        time.sleep(.25)
        self.assertFalse(marker.exists(), 'same-group descendant survived successful verifier exit')
        self.check_clean()

    def test_deadline_prevents_replenishing_after_completed_work(self):
        result = race(self.repo, [Candidate('hang', 'import time; time.sleep(30)\n'+GOOD, 0),
                                 Candidate('never-launch', GOOD, 0)],
                      mode='serial', deadline=.06)
        self.assertEqual(result['synthetic_reserved_tokens'], 100)
        self.assertNotIn('never-launch', [e.get('candidate') for e in result['events']])
        self.check_clean()

    def test_worktrees_are_separate(self):
        # One candidate needs to observe only its own contents while another overwrites its own file.
        source = 'from pathlib import Path\nassert "own-marker" in Path(__file__).read_text()\n# own-marker\n'+GOOD
        result = race(self.repo, [Candidate('own', source, .01), Candidate('other', 'raise RuntimeError("other")', 0)], concurrency=2)
        self.assertEqual(result['winner'], 'own')
        self.check_clean()

    def test_cost_budget_reserves_cancelled_work_conservatively(self):
        result = race(self.repo, FIXTURES, concurrency=3, cost_budget_microdollars=200)
        self.assertIsNone(result['winner'])
        self.assertEqual(result['synthetic_estimated_microdollars'], 200)
        self.check_clean()

    def test_budget_validation(self):
        with self.assertRaises(ValueError):
            Budget(-1)
        with self.assertRaises(ValueError):
            race(self.repo, [], concurrency=0)
        with self.assertRaises(ValueError):
            race(self.repo, [Candidate('bad', GOOD, -1)])

if __name__ == '__main__':
    unittest.main()
