# Test strategy rubric

Reference material for the test-strategy subagent. Read before drafting the strategy.

## Sources of truth

Identify every available source that informs what "correct" means for this task. Stack-rank them by importance first, reliability second, and state what each one covers and where it has gaps:

- The user's description of what they want and any documents they reference (the top priority — flag what's ambiguous, contradictory, or underspecified)
- A running reference implementation (strongest for ports/rewrites — can compare outputs mechanically; state whether it's actually runnable on this machine)
- External documentation that can be fetched (helpful — API docs, library references, tutorials with expected behavior)
- Internal documentation like specifications and documentation (strong — can derive test cases; but may only cover part of the work)
- An existing test suite in the codebase (useful — captures known expectations; BUT may itself be incomplete or wrong)
- Conventions visible in the existing codebase (weakest — inferred expectations, useful for consistency)

## Existing automated evidence

Identify every relevant automated check that already exists for this task: unit, integration, scenario, E2E, smoke, replay, browser-driven, monitoring-backed reproduction, or other reproducible verification.

For each one, state:

- whether it already exists and is runnable in this environment
- whether it currently passes, fails, or is unknown
- what user-visible behavior it validates
- how much confidence it provides relative to its cost and fidelity
- whether the strategy should reuse it as-is, extend it, or supplement it with new tests

Prefer high-value existing automated checks when they already verify the right user-visible behavior. Recommend writing new tests wherever the existing suite leaves meaningful gaps in fidelity, diagnosis, speed, or coverage.

If the transcript, bug report, or other task context already identifies automated checks that are red and need to go green, call them out explicitly and include them in the recommended strategy.

## Harnesses

Identify what test infrastructure exists and what might need to be built or strengthened. There are usually several at different levels:

- **Direct API harness**: Can tests call into the code as a library? This is useful for narrow logic checks and fast feedback, but it does not by itself prove the user-observable behavior is correct. State clearly what it can validate and what it cannot.
- **Programmatic state harness**: Can the system expose structured state for assertions? For a game: player position, inventory, level layout. For a web app: DOM state or API responses. For a service: database contents. This is valuable for precise assertions and debugging, but it should support user-visible tests rather than replace them. State the cost to build.
- **Interaction harness**: Can tests drive the system the way a user would through the real product surface: browser, CLI, HTTP, file import/export, simulated keystrokes? This is usually the highest-value harness because it verifies real behavior instead of mocked behavior. State what boot/teardown infrastructure is needed.
- **Output capture harness**: What user-visible outputs can tests observe? Screen buffer as text, rendered HTML, browser snapshots, screenshots, logs, network traffic, output files. Prioritize observation surfaces that match what the user actually sees or consumes. State what interpretation is needed.
- **Reference comparison harness**: If a reference exists, can we run both with identical inputs and diff outputs? State whether the reference is runnable and what tooling is needed to compare.

For each harness, state whether it exists already, what it would cost to build or strengthen, and what class of tests it enables. Recommend which to invest in based on what coverage they unlock relative to their cost, with the highest weight on real interaction plus user-visible output capture. Avoid mock-heavy strategies unless a real dependency truly cannot be exercised.

## Verification approach

Based on the sources of truth and harnesses, describe what testing looks like for this task. Frame this as: what tests will drive the quality needed to accomplish the user's goals. There should be at least one test that validates that what the user asked for, actually occurs. If the user asks for something to appear on screen; it should include a screen capture. If the user asked for something to happen with AI, that AI should be called. If this would be expensive or impractical, escalate to the user.

- **User-behavior confidence**: What evidence would make us confident that a real user would observe correct behavior? Focus on the real product surface first: UI, CLI, HTTP responses, rendered files, or other user-visible outputs.
- **Behavioral coverage**: What can the user do with this system, and how much of that action space should tests exercise through those real surfaces?
- **Integration coverage**: What systems interact with the changed code, and which of those interactions should be tested through the real system rather than mocks or internal seams?
- **Red-to-green targets**: Which existing automated checks are already part of the problem statement or prior evidence, and how should they be used as explicit acceptance criteria?
- **Edge cases and boundaries**: Where are the limits, and which matter for this task?
- **Regression safety**: Does the existing test suite protect what already works, or do we need characterization tests?
- **Failure modes**: What happens when things go wrong, and how much matters here?
- **Performance**: Assess how likely this change is to affect performance and how hard it is to measure. For most changes, a simple timing assertion ("operation completes in under Xms") catches catastrophic regressions cheaply — X should be generous enough that any violation is a severe bug, not noise. For performance-critical work where improvement is the goal, real measurement in a realistic environment is unavoidable — state what that environment is, how to deploy to it, and how to measure safely. Scale the approach to what the risk warrants.
- **Visual/perceptual correctness**: If the change affects what the user sees, you will almost certainly need a screenshot that you inspect. You might need video or even more expensive approaches, and it might be possible to know how it looks without rendering an image, e.g. a CLI output. Recommend the appropriate reproducible observation method that provides meaningful confidence. Do not recommend human validation.

## Test plan emphasis

State clearly how the later test plan should spend its effort:

- Start with the highest-value checks that exercise real user-visible behavior through the actual UI, CLI, HTTP surface, or other outputs the user consumes. Prefer existing checks/harnesses if available, but do not be afraid to specify creating them.
- If the problem statement or prior evidence already identifies automated checks that are red and need to go green, include them explicitly in the plan.
- Reuse or extend existing multi-step scenario tests when they already cover realistic user journeys, and add new scenario or integration tests wherever important user behavior is still weakly covered or uncovered.
- Use reference comparisons, regression tests, and boundary tests to deepen confidence where they buy meaningful signal.
- Use unit tests sparingly, only where isolated logic is genuinely clearer to validate that way. Unit coverage cannot be the main argument for correctness.
- Call out any important user behavior that will remain weakly tested, and explain the residual risk plainly.
