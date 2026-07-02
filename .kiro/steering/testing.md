# Testing Preference

Always add tests when implementing new features or fixing bugs, without waiting
to be asked. This preference was set explicitly by the user (superseding any
earlier "don't add tests unless requested" stance).

Guidelines:
- New feature: add tests covering the primary behavior and the notable edge
  cases (error/empty/failure states), matching the existing test framework and
  patterns in the affected package.
  - Backend (`src/rag_system`): pytest / Hypothesis, under `tests/`.
  - Frontend (`frontendkimchi`): Vitest + Testing Library + MSW, colocated
    `*.test.ts(x)` files.
- Bug fix: add a regression test that fails on the old behavior and passes on
  the fix.
- Run the relevant test suite after writing tests and ensure it passes before
  reporting done.
