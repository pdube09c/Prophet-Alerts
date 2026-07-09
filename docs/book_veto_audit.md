# Book-veto reconciliation audit (provisional)

The book layer implements the confirmed spec (>=1.0 pt consensus home-spread move
toward the dog, sustained 2+ consecutive snapshots, pre-entry window, retail-median
OR Pinnacle). It reproduces the backtest on 91.1% of 628 candidates -- the provable
ceiling for a directional rule. The games below are the discrepancies vs the
`bgpf` ground truth extracted from equity_curves.html. `maxTD` = max displacement
toward the dog (points) observed in each series within the pre-entry window.

Backtest parity is NOT assumed to be the goal: live alerts run on unrounded Odds
API data at full fidelity, and closing this gap may require reproducing a possible
quirk of the original (unavailable) book code. See sports/nba.py for details.

## 44 FALSE NEGATIVES -- backtest vetoed `book`, our rule did not
(No >=1 toward-dog move on disk; several drift toward the favorite. Prime audit set.)

| date | favorite | dog | ml | maxTD retail | maxTD pinnacle |
|---|---|---|---|---|---|
| 2025-11-02 | Los Angeles Lakers | Miami Heat | -176 | 0.75 | 0.5 |
| 2025-11-07 | Miami Heat | Charlotte Hornets | -240 | 0.0 | 0.0 |
| 2025-11-07 | Milwaukee Bucks | Chicago Bulls | -158 | 0.0 | 0.0 |
| 2025-11-12 | New York Knicks | Orlando Magic | -156 | 0.0 | 0.5 |
| 2025-11-21 | Dallas Mavericks | New Orleans Pelicans | -166 | 0.0 | 0.0 |
| 2025-11-28 | Chicago Bulls | Charlotte Hornets | -136 | 0.0 | 0.0 |
| 2025-11-28 | Cleveland Cavaliers | Atlanta Hawks | -210 | 0.0 | 0.0 |
| 2025-11-28 | Detroit Pistons | Orlando Magic | -146 | 0.5 | 0.0 |
| 2025-11-28 | Los Angeles Clippers | Memphis Grizzlies | -240 | 0.25 | 0.0 |
| 2025-11-28 | New York Knicks | Milwaukee Bucks | -320 | 0.0 | 0.0 |
| 2025-12-09 | Orlando Magic | Miami Heat | -107 | 0.25 | 1.0 |
| 2025-12-12 | Dallas Mavericks | Brooklyn Nets | -300 | 0.0 | 0.0 |
| 2025-12-12 | Detroit Pistons | Atlanta Hawks | -265 | 0.0 | 0.0 |
| 2025-12-15 | Los Angeles Clippers | Memphis Grizzlies | -162 | 0.0 | 0.0 |
| 2025-12-18 | Miami Heat | Brooklyn Nets | -240 | 0.0 | 0.0 |
| 2025-12-19 | Cleveland Cavaliers | Chicago Bulls | -235 | 0.0 | 0.0 |
| 2025-12-27 | Phoenix Suns | New Orleans Pelicans | -168 | None | None |
| 2025-12-29 | Denver Nuggets | Miami Heat | -125 | 0.25 | 0.5 |
| 2025-12-29 | Golden State Warriors | Brooklyn Nets | -205 | 0.0 | 0.0 |
| 2025-12-31 | Cleveland Cavaliers | Phoenix Suns | -230 | 0.0 | 0.0 |
| 2026-01-12 | Los Angeles Clippers | Charlotte Hornets | -178 | 0.0 | 0.0 |
| 2026-01-13 | Miami Heat | Phoenix Suns | -107 | 0.0 | 0.0 |
| 2026-01-13 | Oklahoma City Thunder | San Antonio Spurs | -285 | 0.0 | 0.0 |
| 2026-01-16 | Houston Rockets | Minnesota Timberwolves | -164 | 0.5 | 0.0 |
| 2026-01-17 | Boston Celtics | Atlanta Hawks | -148 | 0.5 | 0.0 |
| 2026-01-19 | Detroit Pistons | Boston Celtics | -129 | 0.75 | 0.0 |
| 2026-01-24 | Los Angeles Lakers | Dallas Mavericks | -156 | 0.0 | 0.0 |
| 2026-01-24 | Philadelphia 76ers | New York Knicks | -111 | 0.0 | 0.0 |
| 2026-01-29 | Milwaukee Bucks | Washington Wizards | -123 | 0.25 | 0.0 |
| 2026-02-01 | Phoenix Suns | Los Angeles Clippers | -123 | 0.25 | 0.0 |
| 2026-02-03 | Detroit Pistons | Denver Nuggets | -186 | 0.25 | 0.0 |
| 2026-02-07 | Denver Nuggets | Chicago Bulls | -240 | 0.0 | 0.0 |
| 2026-02-07 | Portland Trail Blazers | Memphis Grizzlies | -315 | None | None |
| 2026-02-09 | Denver Nuggets | Cleveland Cavaliers | -104 | 0.0 | 0.0 |
| 2026-02-09 | Golden State Warriors | Memphis Grizzlies | -335 | 0.75 | 0.5 |
| 2026-02-19 | Orlando Magic | Sacramento Kings | -315 | 0.0 | 0.5 |
| 2026-02-19 | Philadelphia 76ers | Atlanta Hawks | -108 | 0.0 | 0.0 |
| 2026-02-20 | New Orleans Pelicans | Milwaukee Bucks | -158 | 0.0 | 0.0 |
| 2026-02-21 | Phoenix Suns | Orlando Magic | -130 | 0.75 | 0.5 |
| 2026-02-22 | Dallas Mavericks | Indiana Pacers | -119 | 0.0 | 0.0 |
| 2026-02-22 | Denver Nuggets | Golden State Warriors | -250 | 0.0 | 0.0 |
| 2026-02-23 | Detroit Pistons | San Antonio Spurs | -111 | 0.0 | 0.5 |
| 2026-02-24 | Los Angeles Lakers | Orlando Magic | -186 | 0.5 | 0.0 |
| 2026-03-10 | Houston Rockets | Toronto Raptors | -174 | 0.25 | 0.5 |

## 12 FALSE POSITIVES -- our rule vetoed, backtest kept

| date | favorite | dog | ml | maxTD retail | maxTD pinnacle |
|---|---|---|---|---|---|
| 2025-11-18 | San Antonio Spurs | Memphis Grizzlies | -210 | 1.0 | 0.5 |
| 2025-12-12 | Philadelphia 76ers | Indiana Pacers | -265 | 2.0 | 0.5 |
| 2026-01-09 | New York Knicks | Phoenix Suns | -108 | 1.0 | 0.5 |
| 2026-01-12 | Dallas Mavericks | Brooklyn Nets | -150 | 0.0 | 1.0 |
| 2026-01-15 | Orlando Magic | Memphis Grizzlies | -196 | 1.0 | 0.0 |
| 2026-02-02 | Charlotte Hornets | New Orleans Pelicans | -235 | 12.75 | 12.5 |
| 2026-02-06 | Portland Trail Blazers | Memphis Grizzlies | -295 | 1.25 | 0.0 |
| 2026-02-25 | Denver Nuggets | Boston Celtics | -140 | 1.0 | 0.0 |
| 2026-02-26 | Philadelphia 76ers | Miami Heat | -144 | 1.0 | 1.0 |
| 2025-11-03 | Milwaukee Bucks | Indiana Pacers | -210 | 1.0 | 1.0 |
| 2026-03-07 | Minnesota Timberwolves | Orlando Magic | -235 | 2.0 | 0.0 |
| 2026-03-15 | Oklahoma City Thunder | Minnesota Timberwolves | -345 | 1.0 | 0.5 |
