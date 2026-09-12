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
| `CFBDAPIKEY` | CollegeFootballData key (CFB: FBS membership, as-of stats, calendar) |
| `SUPABASEURL` | Supabase project URL                            |
| `SUPABASEKEY` | Supabase `service_role` key (workflow-side)     |
| `EMAILAPIKEY` | Transactional email provider key (alerts/summary) |

Locally, copy `.env.example` → `.env` (git-ignored) and fill in. Non-secret
config lives in `config/settings.example.toml` → `config/settings.toml`.

**Non-secret runtime config** (safe to expose) can also come from GitHub Actions
**Variables** (the `vars` context, *not* Secrets), which override the file so CI
needs no `settings.toml`: `STAGE`, `EMAIL_FROM`, `EMAIL_TO`, `PAGE_URL`,
`STAKE_TARGET`, `STAKE_LADDER`, `NTFY_TOPIC`, `CFB_ENTRY_OFFSET_MINUTES`. `STAKE_LADDER` (comma-separated,
e.g. `100,500,1000,2000,3000,5000`) sets the reference stake ladder shown in each
alert; it mirrors `[nba] stake_ladder` in `settings.toml`, is validated on load
(non-empty, positive, strictly ascending — malformed values fail loudly), and is
per-environment so it can differ by sport later. `NTFY_TOPIC` (mirrors
`[notify] ntfy_topic`) is the [ntfy.sh](https://ntfy.sh) push topic for the short
"go look" notification fired alongside the detail email — use a **long random
string** (the topic is effectively a shared secret; regenerate it if it leaks),
and leave it empty to disable the push (the email still sends). The static page's client values
(`SUPABASE_URL`, the **anon** key, `STAGE`) live in `docs/config.js` — committed
so GitHub Pages can serve it, and holding the anon key only; the `service_role`
key never touches the browser.

## Staged rollout (discipline gates, §11)

- **Stage 0 — paper only.** Full loop, $0 stakes, alerts labeled `PAPER`. Run a
  meaningful stretch and confirm survivors hit forward before any real money.
- **Stage 1 — flat $100 real** — only if Stage 0 corroborates the lean.
- **Stage 2 — scale the stake target** — only if Stage 1 holds over enough bets.

The active stage lives in `config/settings.toml [app] stage`.

## Build status

The full §12 build sequence is wired and unit-tested (158 tests green,
`python -m pytest tests/`). The veto-layer gate (§12.2) was proven **before**
anything else was built. **NBA and CFB both ship** — see
[College football (CFB)](#college-football-cfb).

| §     | Piece                                   | Files |
|-------|-----------------------------------------|-------|
| 12.1  | Hosted-DB schema + client               | `config/schema.sql`, `engine/db.py` |
| 12.2  | Sport contract + NBA adapters + layers  | `sports/base.py`, `sports/nba.py`, `engine/veto.py` |
| 12.3  | Rolling tick (append, in-window, alert) | `engine/tick.py` |
| 12.4  | Stake table + alert + email             | `engine/stake.py`, `engine/alert.py`, `engine/email.py` |
| 12.5  | Static selection page (anon-key, RLS)   | `docs/index.html`, `docs/config.example.js` |
| 12.6  | Daily stats + settle + morning summary  | `engine/daily_stats.py`, `engine/settle.py`, `engine/summary.py` |
| 12.7  | The GitHub Actions workflows            | `.github/workflows/{tick,cfb-tick,daily-stats,morning-summary,cfb-validate}.yml` |
| 12.8  | Stage 0 paper loop (offline e2e)        | `tests/test_end_to_end.py` |
| —     | CFB plug-in (second sport)              | `sports/cfb.py`, `sports/data/cfb_crosswalk.json`, `tools/` |

The tick is **idempotent and self-healing**: all state lives in the hosted DB
(never on the ephemeral runner), and the `alerted` flag guarantees each survivor
alerts at-least-once and never duplicates once the flag lands. `engine/tick.py`,
`engine/settle.py`, and the alert/summary composers isolate the network + DB
boundaries so the whole loop is tested with fakes (`tests/test_tick.py`,
`tests/test_alert.py`, `tests/test_settle_summary.py`, `tests/test_end_to_end.py`).

### Deploy

1. Apply `config/schema.sql` once in the Supabase SQL editor (tables + RLS).
2. Set the four Secrets + non-secret Variables above in the GitHub repo.
3. The selection page is served by **GitHub Pages from `/docs`** (Settings →
   Pages → source: `main`, folder `/docs`). `docs/config.js` is already
   committed with the anon key + URL (`docs/config.example.js` is the template);
   edit it there if those change. Put the published Pages URL in `PAGE_URL`.
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

6. **Test the bets-logging round trip** with the seeded-smoke mode. Unlike
   `--smoke` (which is DB-free), this **writes to Supabase**: it inserts one
   synthetic survivor and sends its alert, so you can click the email link, see
   the survivor render on the page, and Log a bet end-to-end.
   - **Seed:** `python -m engine.alert --seed-smoke` — needs `SUPABASEURL` /
     `SUPABASEKEY` (service_role) plus the email vars, and set **`PAGE_URL`** so
     the email's "Log your decision" link points at your live page. The row is
     written with a future tip (so it clears the page's `tip > now-3h` filter),
     `alerted=True` (so a real tick won't re-alert it), and a `SMOKE-TEST:`
     `game_id` prefix. Run it **locally** — the CI `force_sample` dispatch wires
     only email config, not the DB secrets this needs.
   - **Clean up:** `python -m engine.alert --seed-smoke-clean` — deletes every
     seeded survivor (scoped strictly to the `SMOKE-TEST:` prefix) and any bets
     logged against the synthetic teams. Nothing real is ever touched.
   - **Scope:** this verifies the trip only **up to bet-insert**; it does *not*
     test settlement/grading, because the Odds API has no scores for fake teams.

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

## College football (CFB)

CFB is a **second sport plug-in**, not a second app. It adds a data source and
evaluation logic; everything downstream of a surviving candidate — the stake
ladder, the three-stage sizing, alert compose/send, the ntfy push, the
bets-logging round trip, the selection page and settlement — is the
sport-agnostic engine, reused unchanged.

### The strategy

Bet the **favorite moneyline on ProphetX** when every condition holds.

**Universe**

- **FBS vs FBS**, with membership from CFBD `/teams/fbs?year=YYYY` **at
  runtime** — never hardcoded. Reclassifiers (North Dakota State, Sacramento
  State in 2026) are picked up automatically and departures drop automatically;
  a hardcoded list is what caused silent drops last season.
- Favorite ML in **[-300, -100]** — nothing heavier than -300.
- **Week 3+.** Week 5+ is the validated range, but Weeks 3-4 are **not gated
  off**: they fire with an explicit data-maturity flag instead (below).

**The three conditions** — the favorite must pass all three.

| # | Layer | Vetoes when |
|---|-------|-------------|
| 1 | `stuff` | the underdog has a **top-quartile defensive stuff rate** across the as-of FBS population |
| 2 | `retail-extend` | **>=2 of the 4 retail books** each show a *sustained* >=1pt move toward the favorite off **their own opener**, between listing and **T-48h** before kickoff, **and Pinnacle does not** show a >=1pt move over the same window |
| 3 | `dog-SR` | the underdog's offensive **success rate does not exceed** the favorite's (strict `>`) |

"Sustained" = the >=1pt condition holds across **2 consecutive hourly
snapshots** — not a one-poll blip that reverts. The retail-extend window is
**kickoff-relative**, so Saturday games and weeknight MACtion are handled by
identical logic with no special-casing. (The veto is unvalidated on weeknight
games — the 2025 sample was too thin — and runs there by explicit decision.)

### Point-in-time stats (no lookahead)

Stats are CFBD **as-of**, **garbage-time-excluded**, through the **prior week**:
a week-N game is evaluated on `endWeek=N-1`.

Two guards enforce this, and both **raise** — neither warns, and nothing catches
them. A lookahead-contaminated evaluation stops the tick rather than quietly
emitting an alert built on a leaked result.

- `assert_no_lookahead(game_week, endweek)` checks the *arithmetic*. On its own
  this is weak, because `stats_asof_key` derives `endweek` as `wk - 1` by
  construction and so can never trip it; it bites only for a caller that
  computes `endweek` independently (`tools/validate_cfb.py` does).
- `assert_stats_are_asof(game_week, team_stats)` checks the *data*, and is the
  one that actually fires. `pull_stats` stamps every row with the `endWeek` it
  was pulled through, so this catches what the arithmetic cannot see: a mis-filed
  as-of key, a backfill run with the wrong `endWeek`, a season-final pull written
  over the weekly rows, or a shifted calendar boundary. It inspects **every team
  in the view**, not just the two playing — one contaminated row would move the
  stuff-rate quartile threshold and change the verdict on games it is not part
  of. Week boundaries come from CFBD
`/calendar?year=YYYY`, so a shifted Week 0 or a 15-week season needs no code
change. CFBD's `nationalAverages` sentinel row is excluded from every quartile
population.

### The data-maturity flag

Because alerts fire from Week 3 but the stats conditions are not reliable until
~Week 5, **every CFB alert carries a maturity flag** so no alert overstates
itself. It annotates; it never gates.

```
Data maturity    : as-of through Week 2: 2 games of data
Stats confidence : noise-regime (validated from Week 5+)
Retail-extend    : complete - 14 snapshots through T-48h
```

Games-of-data is counted from the CFBD schedule (so byes are handled) and
reports the **thinner** of the two teams. `Retail-extend` is `complete` only
when the T-48h window has actually closed *and* enough snapshots accumulated to
judge a sustained move; otherwise `partial`, with the reason.

### Pipeline

Both jobs fall out of the **existing** self-healing rolling tick — nothing
re-implements it (`.github/workflows/cfb-tick.yml`, hourly, every day):

- **accumulate** — every tick appends the whole board's per-book spread + h2h
  snapshot to Supabase. That accumulated trajectory *is* what the retail-extend
  veto reads later; there is no separate collector. One board-wide `/odds` pull
  per tick (~2 credits) — `todays_games` and `pull_odds_snapshot` share one
  cached payload.
- **fire** — a game reaching **T-24h** (`CFB_ENTRY_OFFSET_MINUTES`, default
  1440) is evaluated against the three conditions and, surviving, alerted
  exactly once through the existing alert path.

### Team-name crosswalks

The hand-verified name mappings live in the sibling repo
`odds-backtest-verification` (`src/ncaaf-crosswalk.ts`, `src/cfbd-crosswalk.ts`),
which remains the **single source of truth**. Because CI has no access to that
repo, they are **compiled** into `sports/data/cfb_crosswalk.json`:

```bash
python -m tools.gen_cfb_crosswalk          # regenerate after editing the .ts
python -m tools.gen_cfb_crosswalk --check  # fail if the checked-in JSON is stale
```

`tests/test_cfb.py` re-runs the generator whenever the source repo is present
and fails on divergence, so the copy cannot drift silently.

**Exact match only — no fuzzy matching, no prefix fallback.** A prefix rule
would be actively wrong here ("Ohio" is the Bobcats, "Ohio State" the Buckeyes).
The hard errors sit where they matter:

- an unmapped CFBD **FBS** school raises in `CFB.fbs()`, so the universe can
  never silently shrink;
- an unresolvable **board** name is announced loudly on stderr and that one game
  is skipped — raising there would let a single unknown FCS opponent suppress
  alerts for the entire board;
- `tools/validate_cfb.py` **fails** on any unresolvable name and is the
  pre-flight gate.

### Before going live

```bash
# 1. All three checks: crosswalk resolution, the CFBD as-of join, and the
#    retail-extend veto reading the accumulated Supabase trajectory.
python -m tools.validate_cfb            # --skip-db for checks 1-2 only

# 2. Synthetic CFB alert through the REAL SendGrid path. DB-free, $0.
python -m engine.alert --smoke --sport cfb

# 3. Seeded round trip: writes a synthetic survivor, sends its alert, and the
#    page link renders it so a bet can be logged by hand.
python -m engine.alert --seed-smoke --sport cfb
python -m engine.alert --seed-smoke-clean
```

`cfb-validate` and `cfb-tick` (with `force_sample: true`) run the first two from
the Actions tab, proving the CI secrets and the CFB email path.

### Running CFB by hand

```bash
python -m engine.tick --sport cfb          # one accumulate + fire tick
python -m engine.daily_stats --sport cfb   # as-of stats, last completed week
python -m engine.settle --sport cfb        # grade yesterday's CFB bets
python -m engine.summary --sport cfb       # the CFB morning summary
```

Alerts, pushes and summaries are **sport-tagged** (`[PAPER] [CFB] ...`) and the
selection page shows a per-card sport badge, so NBA and CFB never blur together.
The `bets` table has carried a `sport` column since the original schema, so CFB
bets are tracked separately with no migration.
