# Fast Forward

A budgeted candidate patch racing laboratory inspired by fast inference infrastructure. Independent prototype; not affiliated with Cerebras.

**Status:** executable local prototype with a generation-only Cerebras HTTP adapter, offline contract tests and paired recorded-candidate validation. No live-key benchmark has been run, no cloud deployment exists, and no Cerebras speedup is claimed.

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

The verifier checks only a few authored cases. Passing it is evidence for those cases, not proof of general correctness. There is no second hidden model benchmark, adaptive scheduler, browser interface, or arbitrary-repository support yet.

## Quarantined provider generation

The adapter sends one non-streaming request to the fixed official `https://api.cerebras.ai/v1/chat/completions` endpoint. It requires an explicit model and a completion cap (1–8192), uses strict JSON Schema with `additionalProperties: false`, and accepts only `{path: "solution.py", source: "..."}`. Authentication is read only from `CEREBRAS_API_KEY`; the CLI has no key argument. It refuses redirects, bounds response/source/task sizes and request timeout, emits fixed error codes, and never stores upstream error bodies or exception strings. No retries are automatic. The cap bounds completion tokens, **not total spend or prompt tokens**; model access, rates and prompt charges still apply.

The following command makes a paid request when you supply your own environment key. It **saves source as data and never executes it**:

```bash
# Set CEREBRAS_API_KEY securely in your environment first.
python -m fast_forward generate --model YOUR_AVAILABLE_MODEL --max-completion-tokens 512 --task task.txt --output candidate.json
python -m fast_forward recorded-view candidate.json
```

Nonaccepted generations save sanitized failure records before returning exit code 2. Refusal/truncation records retain valid reported usage. Missing, malformed, oversized or unavailable usage is explicitly unknown; it is never treated as free. Client timeout or disconnect does not prove provider cancellation or eliminate billing. There is no streaming cancellation or concurrent live generation yet. Records contain source and model identity: keep them private when prompts or generated code are sensitive.

## Paired prerecorded validation

These commands are offline and need no key:

```bash
python -m fast_forward recorded-fixture --output candidates.json
python -m fast_forward recorded-view candidates.json
python -m fast_forward paired-replay candidates.json --output paired.json
```

`recorded-view` performs no subprocess, network, import or candidate execution. `paired-replay` executes only the **exact three authored source strings already built into this repository**. It rejects all other source, including valid generated JSON; there is no override flag. Real-provider records can be inspected safely and can be replayed only if their candidate source exactly matches that small allowlist. Extending this to novel model-generated patches requires a real execution sandbox first.

Both replay modes receive identical prerecorded candidates, one base commit and the same protected verifier. Generation delays are zero during replay. `validation_elapsed_seconds` includes validation scheduling, worktree setup, subprocess verification and cleanup. Prior generation timings are grouped by origin: actual client times and scripted synthetic delays remain separate. Their sums are not parallel critical paths. The report counts **all supplied** generation records once, including failed generations and losing/unvalidated candidates, rather than charging only the winner or duplicating usage for two validation modes. Records absent from the input cannot be accounted for.

Usage is grouped separately as `provider_reported`, `mock_reported`, `synthetic`, or `unknown`. Imported origin labels are self-reported metadata, not cryptographic proof of real calls; injected transports are always labeled mock. No dollar estimate is made without versioned pricing. Checked-in `docs/recorded-fixture.json` and `docs/paired-validation.json` contain **synthetic generation inputs with measured local validation**, never live-provider performance evidence. Replay does not measure end-to-end inference speedup, and can be slower in race mode for tiny tasks.

Imported recordings allow at most 100 records and generation timings from zero to 86,400 seconds per record. These local evidence limits reject implausible numeric values and keep aggregate timings finite standard JSON.

## Language choice and next milestone

Python currently orchestrates HTTPS, JSON validation, Git and subprocess lifecycles using the standard library. This repository does not implement an inference runtime or inference kernels; those run on the provider. A C++ worker would make sense for native CPU/GPU inference, profiling-sensitive verification, or a measured scheduling bottleneck. Rewriting the HTTP orchestration in C++ would not substantiate an inference improvement here.

Next work is sandboxed execution of generated candidates, a larger held-out task suite and authorized live repeated paired calls with pinned models, full prompt/output usage, billed cost and provider cancellation evidence. There is no measured native inference worker in this milestone.

See [research and specification](docs/research.md) for sources and the evaluation plan.
