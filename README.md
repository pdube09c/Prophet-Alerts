# Multi-Sport Betting Alert App (Design B: serverless)

On a schedule, pull odds + point-in-time stats, evaluate each favorite-ML
candidate against a sport's veto layers, alert the survivors ~1:55 before each
game, let you log which bets you placed, then settle and email a results summary
the next morning. GitHub Actions is the always-on scheduler + compute; a hosted
DB (Supabase) holds all state; a static page is the phone-facing selection UI.
Built sport-agnostic — NBA ships first, college football plugs in later.

## Three rules this app lives by (do not lose these)

1. **It never places bets.** It surfaces survivors and the live price; you place
   the bet manually on ProphetX. No ProphetX credentials are stored anywhere.
2. **It logs and alerts; it does not decide.** The veto logic filters; you choose
   whether and how much to bet.
3. **This is a paper-trade instrument first.** The backtested edge is in-sample
   on one contaminated season — a *lean*, not a proven forward edge. Stage 0
   runs the full loop at $0/paper to test whether the lean survives contact with
   live markets before any scaled real money (see the staged rollout below).

## Secrets

Read only from environment variables — never hard-coded, never committed. In
production these are **GitHub Actions Secrets**:

| Secret        | Use                                             |
|---------------|-------------------------------------------------|
| `ODDSAPIKEY`  | The Odds API key                                |
| `SUPABASEURL` | Supabase project URL                            |
| `SUPABASEKEY` | Supabase `service_role` key (workflow-side)     |
| `EMAILAPIKEY` | Transactional email provider key (alerts/summary) |

Locally, copy `.env.example` → `.env` (git-ignored) and fill in. Non-secret
config lives in `config/settings.example.toml` → `config/settings.toml`.

**Non-secret runtime config** (safe to expose) can also come from GitHub Actions
**Variables** (the `vars` context, *not* Secrets), which override the file so CI
needs no `settings.toml`: `STAGE`, `EMAIL_FROM`, `EMAIL_TO`, `PAGE_URL`,
`STAKE_TARGET`, `STAKE_LADDER`. `STAKE_LADDER` (comma-separated, e.g.
`100,500,1000,2000,3000,5000`) sets the reference stake ladder shown in each
alert; it mirrors `[nba] stake_ladder` in `settings.toml`, is validated on load
(non-empty, positive, strictly ascending — malformed values fail loudly), and is
per-environment so it can differ by sport later. The static page's client values
(`SUPABASE_URL`, the **anon** key, `STAGE`) live in git-ignored `web/config.js` —
the `service_role` key never touches the browser.

## Staged rollout (discipline gates, §11)

- **Stage 0 — paper only.** Full loop, $0 stakes, alerts labeled `PAPER`. Run a
  meaningful stretch and confirm survivors hit forward before any real money.
- **Stage 1 — flat $100 real** — only if Stage 0 corroborates the lean.
- **Stage 2 — scale the stake target** — only if Stage 1 holds over enough bets.

The active stage lives in `config/settings.toml [app] stage`.

## Build status

The full §12 build sequence is wired and unit-tested (23 tests green,
`python -m pytest tests/`). The veto-layer gate (§12.2) was proven **before**
anything else was built.

| §     | Piece                                   | Files |
|-------|-----------------------------------------|-------|
| 12.1  | Hosted-DB schema + client               | `config/schema.sql`, `engine/db.py` |
| 12.2  | Sport contract + NBA adapters + layers  | `sports/base.py`, `sports/nba.py`, `engine/veto.py` |
| 12.3  | Rolling tick (append, in-window, alert) | `engine/tick.py` |
| 12.4  | Stake table + alert + email             | `engine/stake.py`, `engine/alert.py`, `engine/email.py` |
| 12.5  | Static selection page (anon-key, RLS)   | `web/index.html`, `web/config.example.js` |
| 12.6  | Daily stats + settle + morning summary  | `engine/daily_stats.py`, `engine/settle.py`, `engine/summary.py` |
| 12.7  | The three GitHub Actions workflows      | `.github/workflows/{tick,daily-stats,morning-summary}.yml` |
| 12.8  | Stage 0 paper loop (offline e2e)        | `tests/test_end_to_end.py` |

The tick is **idempotent and self-healing**: all state lives in the hosted DB
(never on the ephemeral runner), and the `alerted` flag guarantees each survivor
alerts at-least-once and never duplicates once the flag lands. `engine/tick.py`,
`engine/settle.py`, and the alert/summary composers isolate the network + DB
boundaries so the whole loop is tested with fakes (`tests/test_tick.py`,
`tests/test_alert.py`, `tests/test_settle_summary.py`, `tests/test_end_to_end.py`).

### Deploy

1. Apply `config/schema.sql` once in the Supabase SQL editor (tables + RLS).
2. Set the four Secrets + non-secret Variables above in the GitHub repo.
3. Copy `web/config.example.js` → `web/config.js`, fill in the anon key + URL,
   and host `web/` on any static CDN; put its URL in `PAGE_URL`.
4. The workflows self-schedule (tick every 5 min in-window, daily stats before
   the slate, summary each morning). Start in `STAGE=paper`.
5. **Smoke-test the alert path** before trusting the schedule. This forces one
   synthetic `[SMOKE TEST]` alert through the real SendGrid path — fake team
   names, no DB reads/writes, no live game required — so a failure points only
   at the email path (key, sender verification, deliverability):
   - **In CI (proves Secrets + email end-to-end):** Actions → **tick** → **Run
     workflow** → toggle **force_sample** on. Runs `engine.alert --smoke` with
     only the email env wired; scheduled ticks are unaffected.
   - **Locally:** `python -m engine.alert --smoke` (needs `EMAILAPIKEY` plus
     `EMAIL_FROM` / `EMAIL_TO` / `STAGE` in the env or `settings.toml`).

   The send fails unless `EMAILAPIKEY` is set and `EMAIL_FROM` is a **verified
   SendGrid sender** — which is exactly the misconfiguration this surfaces.

### Veto-layer reconciliation

**Reconciliation result** (see `tests/test_veto_layers.py`):

- The three **stat layers reproduce the backtest exactly**, up to two documented
  data artifacts (neither a metric bug): a handful of median ties caused by the
  *rounded* stat JSONs, and the first game-day (early-season, <6 GP teams).
- The **book layer is PROVISIONAL, reproducing 91.1%** of decisions — the
  provable ceiling for a directional rule (toward-dog 91.1% vs favorite-only 38%,
  bidirectional 62%, unsigned home-line 62–67%). Its direction is confirmed exact
  (fired ⇔ the favorite's line moves toward the dog). The 56 discrepancies (44
  the backtest tagged `book` with no toward-dog move on disk, 12 the reverse) are
  logged in [`docs/book_veto_audit.md`](docs/book_veto_audit.md) as a known audit
  item.

  **Backtest parity is not assumed to be the goal here.** Live alerts pull
  **unrounded** Odds API data, so the forward book veto runs at full fidelity —
  it is not limited by the rounded, fixed-grid historical snapshots that cap the
  reconstruction. Closing the 44-game gap would likely mean reproducing a
  possible quirk of the original (unavailable) book code, which is not
  necessarily desirable. The layer ships as-is and is revisited only if that
  original code or a richer opening-line snapshot history surfaces.

### Running the veto tests

The tests read the real point-in-time data from the sibling
`odds-backtest-verification` project. Point elsewhere with `BACKTEST_ROOT`.

```
pip install pytest
python -m pytest tests/ -v          # assertions
python tests/test_veto_layers.py    # human-readable reconciliation report
```

### Open item — book layer (audit)

The book veto ships provisional at 91.1% (see above and
[`docs/book_veto_audit.md`](docs/book_veto_audit.md)). It runs at full fidelity
forward on unrounded Odds API data; backtest parity is not a goal. Revisit only
if the original book code or an earlier opening-line snapshot history surfaces.
