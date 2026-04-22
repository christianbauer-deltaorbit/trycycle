# Test plan rubric

Reference material for the test-plan subagent. Read before finalizing the test plan.

## Performance

If the agreed strategy includes performance testing, write tests proportional to the risk and practical to execute:

- **Low performance risk**: A simple timing assertion ("this operation completes in under Xms") catches catastrophic regressions cheaply. X should be generous enough that any violation indicates a severe bug, not normal variance.
- **Medium risk**: Benchmark before/after with statistical significance. State what environment the benchmark runs in.
- **Performance-critical work** (the task IS about performance): The strategy should specify the measurement environment (local, staging, production). Write tests targeting that environment. If production measurement is needed, include a safe deployment and rollback plan.

Do not skip performance testing because it's hard. Do scale the approach to what the risk warrants.

## What NOT to write

- **Tautological tests.** If you find yourself reading the implementation to determine expected output, stop. Go back to the source of truth. A test derived from the code proves nothing about correctness. This is the most common failure mode — actively guard against it.
- **Vague tests.** "Verify it works correctly" is not a test. "After pressing `>` on a `>` tile, `game.level` increases by 1 and `game.player.pos` is on a passable tile on the new level" is a test.
- **Implementation-coupled tests.** Assert against behavior and interfaces, not internal state or private methods. The test plan must be compatible with TDD: establish the red state with the highest-value existing relevant check when one exists, otherwise write the missing failing test first, then implement to green. This means tests must be writable before the implementation exists.
- **Mock-behavior tests.** Do not treat mocked collaborators as proof that the system works. If the product can be exercised against the real UI, real outputs, or real adjacent components in the test environment, do that instead.
- **Human-validation tests.** Do not write plan steps that require a person to inspect the UI or decide pass/fail. Convert them into artifact-based checks, preferring browser snapshots or screenshot diffs when structured assertions are insufficient.
- **Existence tests.** Asserting that a UI element is rendered, a route is registered, or a command appears in `--help` does not verify behavior. Every interactive element in the action space must be tested through activation — what happens when the user clicks, submits, or calls it. "Button is visible" is not a test. "Clicking the button opens the correct configuration form" is a test.
- **Tests without a source of truth.** If you cannot name which source of truth (reference implementation, spec, API docs, user description) justifies a test's expected outcome, the test is speculative. Delete it or document the assumption only if it materially affects cost or scope.

## Harness requirements

If the agreed strategy calls for building or strengthening test harnesses, include a section at the top of the plan specifying:

- What each harness does
- What it exposes (programmatic API, state inspection, input simulation)
- Estimated complexity to build
- Which tests depend on it

The harness work is done first, before the tests that depend on it. Without it, the tests that matter most (scenarios, integration) cannot be written or extended effectively.
