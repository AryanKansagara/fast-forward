# Research and first-release specification

Research checked October 29, 2026. This is an engineering experiment, not a claim about confidential roadmaps or novel speculative execution.

## Why this problem

Cerebras describes harness and tool execution overhead as a bottleneck once inference accelerates. Fast Forward asks a bounded question: when does racing candidate patches reduce time to a verified solution enough to justify additional token and worker cost?

Primary source: https://www.cerebras.ai/blog/a-smarter-model-won-t-save-your-bad-harness

## Existing overlap

TomasuLLM already speculates future tool actions in isolated environments and validates before committing results. Pydantic AI documents speculative programmatic tool calling. Fast Forward's narrower emphasis is independent candidate patch races, explicit budget admission, conservative cancellation accounting, and inspectable evidence. Do not describe it as the first speculative agent runtime.

- https://arxiv.org/abs/2609.38201
- https://github.com/pydantic/pydantic-ai/blob/main/docs/harness/code-mode.md

## API feasibility

Cerebras documents strict schemas, multi-turn calls, and parallel tool calling for selected shared-inference models. Availability and model IDs must be checked at integration time. Native SDKs and an OpenAI-compatible `/v1` endpoint exist. Rate limits include request and token dimensions and apply across an organization. Chat responses expose usage and timing information, while application wall time must still be measured separately.

- https://inference-docs.cerebras.ai/capabilities/tool-use
- https://inference-docs.cerebras.ai/quickstart
- https://inference-docs.cerebras.ai/resources/openai
- https://inference-docs.cerebras.ai/api-reference/chat-completions
- https://inference-docs.cerebras.ai/support/rate-limits
- https://git-scm.com/docs/git-worktree

The generation-only adapter now implements the fixed HTTPS endpoint; all verification so far uses offline mocks and synthetic recordings. A real call needs a securely configured key, model entitlement, network reachability, and authorized spend. Candidate environments must never inherit that key. Generated source is quarantined and cannot be executed by the adapter or safe viewer.

Provider contract rechecked October 31, 2026 against the official pages below: strict structured output uses `response_format.json_schema.strict: true`, every object schema sets `additionalProperties: false`, `max_completion_tokens` caps completion (including reasoning), and usage includes `prompt_tokens`, `completion_tokens`, and `total_tokens`. The model is explicitly supplied at runtime rather than assuming an evergreen model identifier. Local byte and token caps are implementation limits, not advertised provider limits.

- https://inference-docs.cerebras.ai/capabilities/structured-outputs
- https://inference-docs.cerebras.ai/api-reference/chat-completions

The paired replay track isolates local validation from prior generation: one prerecorded bundle, one pinned base, serial and race validators, all supplied usage counted once by provenance. It only executes exact authored fixture strings. Imported provenance is self-reported; no checked-in report represents an authenticated live-provider benchmark.

## Implemented architecture

CLI -> scheduler -> token/cost admission -> bounded worker threads -> separate detached worktrees -> patch application -> verifier subprocess -> winner/cancellation -> cleanup -> JSON evidence.

The provider supplies trusted, deterministic candidates. Worktree metadata changes are serialized. The verifier is written outside candidate trees; only solution.py can be patched. The repository base is untouched. Each subprocess runs with a minimal environment and Python isolated mode, but these precautions are not a hostile-code sandbox.

## Evaluation design

The first fixture contains one fast wrong patch, one slow valid patch, and one faster valid alternative. Serial and race modes receive exactly those candidates. This makes scheduler mechanics observable without conflating them with language-model randomness.

Next, author 8–12 tasks with protected checks across parsing, boundary behavior, stale async state, grouping, and performance tradeoffs. Use two experiment tracks:

1. Fixed candidate patches with scripted delays to isolate scheduling behavior.
2. Real repeated model calls with pinned repo commits, identical model, prompts, candidate strategies, resource limits, and paired run ordering.

Report verified success, elapsed time, API generation time, verifier time, all input/output tokens, billed or estimated costs, peak workers, worker-seconds, cancellation latency, rejected patches, unknown usage, and cleanup failures. Never report a latency win without its extra compute cost.

## Prototype acceptance checks

- Incorrect early candidate rejected; later valid candidate wins.
- Serial stops after a valid candidate.
- Budget exhaustion yields explicit unsolved state.
- Path traversal and verifier-file replacement rejected.
- Invalid Python fails actual verification.
- Hanging verification times out and cleans up.
- Global deadline cancels generation and prevents later budget reservations or worker launches.
- Original verifier process group is killed on cancellation, timeout, and success; direct children are reaped. Detached descendants are outside this bounded guarantee.
- A subprocess left behind by a successful verifier cannot perform a delayed marker write.
- Distinct worktrees retain their own patch content.
- Original repository unchanged after all outcomes.
- Synthetic token/cost caps enforced with conservative cancellation charging.

## Deployment boundary

A future web interface can run separately from the execution worker. Public visitors initially select only bounded authored fixtures. Containerized worker execution belongs on a persistent compute service. Real model benchmark results remain blocked on credentials and authorized spend; deployment remains intentionally deferred.
