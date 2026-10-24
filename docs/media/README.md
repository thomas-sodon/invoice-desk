# Media

| File | What it shows | Source |
|---|---|---|
| [compare-mock.png](compare-mock.png) | "Basic AI vs InvoiceDesk" on the 25-invoice demo batch | `apps/web` **offline mock mode** (`pnpm --filter web screenshots`). The batch outcomes are scripted from the demo gold labels, so they are not a measurement. |
| [hidden-text-mock.png](hidden-text-mock.png) | White-text bank-change instruction revealed in a PDF, with the approval panel | same mock mode; the bank numbers are masked to the last 4 |
| [faint-text-mock.png](faint-text-mock.png) | Faint-text instruction revealed in a (simulated) phone photo | same mock mode |

Not here yet (see [GAPS.md](../GAPS.md)):

- `hero.png` (1000x750). The web app's hero screenshot is not reused: its score tiles show
  sample values from the mock fixtures, not values from an eval report.
- `demo.gif`, the 90 s video and the Loom walkthrough.
- `scorecard.png`, which waits for a live run.
