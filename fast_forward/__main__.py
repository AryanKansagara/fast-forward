import argparse
import json
import platform
import sys
from pathlib import Path
import tempfile
from .core import FIXTURES, create_fixture, race


def main():
    parser = argparse.ArgumentParser(description='Measured scheduler demo; synthetic provider, real patch execution')
    parser.add_argument('--mode', choices=['serial', 'race', 'compare'], default='compare')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--token-budget', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cost-budget-microdollars', type=int)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)/'fixture'
        create_fixture(repo)
        modes = ['serial', 'race'] if args.mode == 'compare' else [args.mode]
        runs = [race(repo, FIXTURES, mode=m, concurrency=args.concurrency, token_budget=args.token_budget, cost_budget_microdollars=args.cost_budget_microdollars) for m in modes]
    result = {'environment': {'python': sys.version.split()[0], 'platform': platform.platform()}, 'fixture': 'normalize-unique-sort-v1', 'notice': 'Synthetic inference and tokens; actual worktrees, Python execution, verification and elapsed time.', 'runs': runs}
    payload = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(payload+'\n')
    print(payload)


if __name__ == '__main__':
    main()
