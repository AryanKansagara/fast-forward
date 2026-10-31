import argparse
import json
import platform
import sys
from pathlib import Path
import tempfile
from .core import FIXTURES, create_fixture, race
from .provider import CerebrasProvider, MAX_TASK_BYTES, ProviderConfigurationError
from .recording import RecordingError, fixture_recording, paired_replay, read_recording, view_recording


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {'generate', 'recorded-view', 'paired-replay', 'recorded-fixture'}:
        return recording_main(argv)
    parser = argparse.ArgumentParser(description='Measured scheduler demo; synthetic provider, real patch execution')
    parser.add_argument('--mode', choices=['serial', 'race', 'compare'], default='compare')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--token-budget', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cost-budget-microdollars', type=int)
    args = parser.parse_args(argv)
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
    return 0


def recording_main(argv):
    parser = argparse.ArgumentParser(description='Generation quarantine and safe prerecorded evidence')
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('generate', help='Paid request; save candidate text without executing it')
    generate.add_argument('--model', required=True)
    generate.add_argument('--max-completion-tokens', type=int, required=True)
    generate.add_argument('--task', type=Path, required=True)
    generate.add_argument('--output', type=Path, required=True)
    generate.add_argument('--timeout', type=float, default=30)
    commands.add_parser('recorded-view').add_argument('recording', type=Path)
    replay = commands.add_parser('paired-replay')
    replay.add_argument('recording', type=Path)
    replay.add_argument('--concurrency', type=int, default=3)
    replay.add_argument('--output', type=Path)
    commands.add_parser('recorded-fixture').add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        exit_code = 0
        if args.command == 'generate':
            with args.task.open('rb') as stream:
                raw = stream.read(MAX_TASK_BYTES + 1)
            if len(raw) > MAX_TASK_BYTES:
                raise ProviderConfigurationError('Task exceeds 32768 bytes')
            provider = CerebrasProvider(model=args.model, max_completion_tokens=args.max_completion_tokens, timeout=args.timeout)
            record = provider.generate(raw.decode('utf-8'))
            result = {'records': [record]}
            summary = view_recording([record])
            exit_code = 0 if record['status'] == 'accepted' else 2
            summary['generation_status'] = record['status']
        elif args.command == 'recorded-fixture':
            result = {'records': fixture_recording()}
            summary = view_recording(result['records'])
        elif args.command == 'recorded-view':
            result = summary = view_recording(read_recording(args.recording))
        else:
            result = summary = paired_replay(read_recording(args.recording), concurrency=args.concurrency)
        if getattr(args, 'output', None):
            args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(summary, indent=2))
        return exit_code
    except (ProviderConfigurationError, RecordingError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print('{"error": "Unable to read or write UTF-8 recording/task file"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
