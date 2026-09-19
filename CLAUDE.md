# Claude repository guide

Pompa Next is a new Panasonic Aquarea + HeishaMon dashboard implementation.
Legacy is evidence only, not a codebase to refactor or preserve.

## Entering the repository

1. Read `docs/CONTEXT.md` first.
2. Read `docs/STATUS.md` second.
3. For the assigned task, open only the relevant sections of `docs/ARCHITECTURE.md`.
4. Open `docs/reference/heishamon/*` only when MQTT topics or HeishaMon behavior matter.

This should provide enough context without a repository-wide scan.
Do not inspect `shockwave9315/pompa` unless the task explicitly asks for comparison or reference evidence.
Never infer reusable architecture, code, API compatibility, schema compatibility, or migration requirements from legacy.

## Authority

- `docs/ARCHITECTURE.md`: implementation and domain source of truth.
- `docs/STATUS.md`: current stage and exclusions only.
- `docs/ROADMAP.md`: high-level future sequence.
- HeishaMon reference files: factual device/topic evidence only.

Backend owns domain truth: parsing, freshness, minute semantics, alignment, aggregation, energy, COP, and derived state.
Frontend displays backend facts and must not independently calculate those concepts.

## Delivery discipline

- Stay within the active stage and avoid hidden feature work.
- Prefer a complete vertical stage to artificial micro-stages.
- Add no speculative abstraction or legacy compatibility layer.
- Keep the recorder/history path independent of future control writes.
- Validate with focused, task-specific checks and tests.
- Do not run browsers, dev servers, or watchers merely as generic validation unless requested.
- Do not add WORKLOG files, daily notes, giant phase documents, or PR narratives to project docs.
- Treat commits and PR descriptions as development history.
- Use feature branches and DRAFT PRs for substantive implementation after Stage 0.

If a decision is absent, state the uncertainty and resolve it from current requirements—not legacy habits.
