# Codex repository guide

Pompa Next is a clean implementation, not a refactor of the legacy source tree.

## Start here

1. Read `docs/CONTEXT.md` for stable project context.
2. Read `docs/STATUS.md` for the current stage and scope.
3. Read only the relevant sections of `docs/ARCHITECTURE.md` for the task.
4. Read `docs/reference/heishamon/*` only for MQTT or topic semantics.

Do not scan the whole repository by default.
For Stage 4/5 work in an area present in legacy, inspect only the corresponding legacy product
behavior as evidence before finalizing scope or design; see `docs/CONTEXT.md`. Other legacy reads
require a specific comparison or evidence need.
Do not mine legacy code for reusable architecture, APIs, schemas, or implementation patterns.

## Sources of truth

`docs/ARCHITECTURE.md` is the architectural source of truth.
`docs/STATUS.md` describes current work only; it is not a chronology.
`docs/ROADMAP.md` defines stage direction.
The HeishaMon files are evidence, not application architecture.

Backend code owns domain semantics, aggregation, alignment, energy, COP, and state derivation.
Frontend code renders backend facts and models; it must not calculate domain truth.

## Working rules

- Build only the assigned stage; do not add hidden feature work.
- Prefer complete vertical stages over artificial micro-stages.
- Avoid speculative abstractions and compatibility layers.
- Preserve `0` as a real value and distinguish it from `NULL` and a missing row.
- Keep control/write behavior isolated from the read-only recorder and history path.
- Use task-specific tests and checks.
- Do not start browsers, development servers, or long-running watchers as generic validation unless requested.
- Do not create WORKLOG-style chronology or per-PR history documents.
- Git commits and PR bodies are the development history.
- After this bootstrap, substantive stages use feature branches and DRAFT PRs.

When requirements conflict or architectural truth is missing, surface the gap rather than guessing from legacy.
