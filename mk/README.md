# Component make fragments

Each component adds its targets in `mk/<component>.mk` (included by the root Makefile), e.g.
`mk/data.mk` adds `seed`, `mk/evals.mk` adds `eval` and `report-card`, `mk/accounting.mk` adds
`demo-reset`. Only add targets that work. Update the command list in CLAUDE.md when you add one.
