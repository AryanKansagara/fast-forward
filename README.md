# Fast Forward

A budgeted candidate patch racing laboratory inspired by fast inference infrastructure. Independent prototype; not affiliated with Cerebras.

**Status:** executable local prototype. Deterministic fixture provider only. No model calls, no cloud deployment, no claimed Cerebras speedup.

Three candidate patches compete to repair a Python function. Every candidate receives a separate detached Git worktree from one pinned commit. A protected verifier outside those worktrees executes the actual candidate code. The first verified result wins; losing work is cancelled and worktrees are cleaned up. The same candidates run sequentially for comparison.

## Run

Requirements: Python 3.11+, Git, Linux/macOS with POSIX process groups. No packages, keys, or network access needed.

```bash
python -m unittest discover -s tests -v
python -m fast_forward
python -m fast_forward --mode race --concurrency 3 --token-budget 300 --cost-budget-microdollars 600 --output run.json
```

`run.json` includes the pinned base commit, winning patch diff, actual event timestamps, actual elapsed time including cleanup, verification outcomes, and synthetic budget accounting. `worktrees_remaining` includes the original fixture repository: the expected clean value is **1**.

## What is real and what is synthetic

| Real execution | Synthetic fixture inputs |
| --- | --- |
| Git worktree creation and removal | Predetermined patch contents |
| Patch application | Scripted candidate generation delays |
| Python subprocess verification | 100-token reservation per candidate |
| Process-group cancellation | Fixed 2 microdollars/token price |
| Wall-clock event and elapsed measurements | Model behavior and inference latency |

Fixture-mode results test the scheduler. They do not evaluate Cerebras, model intelligence, or production coding-agent reliability.

## Recorded local run

An illustrative run is checked in at `docs/fixture-run.json`:

| Mode | Winner | Actual elapsed, including cleanup | Synthetic tokens reserved |
| --- | --- | ---: | ---: |
| Serial | slow-valid | 0.538254 s | 200 |
| Race, concurrency 3 | fast-valid | 0.160943 s | 300 |

The race spent a larger synthetic budget to find the faster candidate. Candidate order and delays deliberately make that tradeoff visible. This single designed fixture is not general performance evidence; results vary by host load. Both modes use identical candidate definitions, baseline commit, and verifier.

## Budget and lifecycle semantics

- Reserve each candidate's known synthetic token maximum before launch.
- Enforce both token and optional estimated-cost caps before admitting work.
- Charge the full reservation for launched candidates, including cancelled work. No speculative refunds.
- A low budget may reject all valid candidates and return an explicit null winner.
- A failed early candidate never wins merely because it finishes first.
- Deadline expiry stops launches and cancels generation or verifier processes.
- The executor waits for cleanup before reporting completion. The original POSIX process group is terminated on every verifier outcome, including success, and the direct child is reaped. Descendants that detach into another session are outside this guarantee.
- Only `solution.py` is an accepted patch target. The baseline and verifier are not candidate patch targets.

## Scope and security boundary

**Git worktrees are file separation, not an OS security sandbox.** This prototype executes trusted, built-in fixtures. A malicious Python patch could access host files or spawn escaped processes; the path allowlist and external verifier do not prevent that. Do not accept arbitrary public submissions or attach production credentials. Production work needs container isolation, resource limits, restricted egress, and a privileged orchestrator separated from untrusted workers.

The verifier checks only a few authored cases. Passing it is evidence for those cases, not proof of general correctness. There is no second hidden model benchmark, adaptive scheduler, browser interface, live API adapter, or arbitrary-repository support yet.

## Next research milestone

Add a Cerebras provider behind the same candidate interface, capture returned token usage and model identity, reserve conservative request bounds, and evaluate repeated paired tasks. Distinguish client cancellation from confirmed server cancellation; never claim aborting a socket eliminates billing. Include serial, fixed race, and eventually transparent adaptive scheduling on a shared success/latency/cost frontier.

See [research and specification](docs/research.md) for sources and the evaluation plan.
