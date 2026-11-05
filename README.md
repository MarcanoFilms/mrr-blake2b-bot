<p align="center"><img src="www/img/logo-192.png" width="140" alt=""></p>

# mrr-blake2b-bot

Rents Blake2B hashrate on [MiningRigRentals](https://www.miningrigrentals.com) (MRR)
and points it at your own [DATUM](https://github.com/OCEAN-xyz/datum_gateway) gateway,
so rented rigs mine blocks that *you* build. Ships with the **MRR Command Center**,
a local web dashboard that shows the gateway's ASICs, your active rentals, the market,
the bot's decisions, lets you rent/extend by hand and **audits every rental**: does the
hashrate you pay for actually reach your gateway?

Built for the Bitcoin BLAKE2b fork (BIP-110), where the whole hashrate market lives on MRR
and renting is the cheapest way to get blocks. It works with any algo MRR sells, though:
set `algo` in the config.

```
python3 -I mrr_bot.py market     # cheapest usable rigs and what the budget buys
python3 -I mrr_bot.py plan       # dry run of the next action (never spends)
python3 -I mrr_bot.py run --live # spend (this is what the systemd timer runs)
python3 -I mrr_bot.py status     # balance + active rentals + last actions
python3 -I mrr_bot.py pools      # pool profile, DNS and reachability of your gateway
```

No dependencies beyond Python 3.10+ and the standard library.

![Overview](docs/img/overview.jpg)

| Rentals | History |
|---|---|
| ![Rentals](docs/img/rentals.jpg) | ![History](docs/img/history.jpg) |

![Market](docs/img/market.jpg)

<p align="center"><img src="docs/img/login.jpg" width="520" alt="Login"></p>

Screenshots have addresses, IPs, rental numbers and balances masked. They show the
dashboard with `bot_ui` off; with the bot on there is one more card with its controls.

## How the bot decides

Every pass (the timer runs one every 15 minutes) the bot:

1. Reads the confirmed MRR balance and computes the budget for this pass:
   `min(confirmed - reserve_btc, max_spend_per_run_btc)`.
2. **Extends** any active rental that ends within `extend_window_hours`, as long as
   the quote fits the budget. Continuity beats a new rental: the rig is already
   connected and proven.
3. If there are free slots (`max_concurrent_rentals`), fetches the Blake2B rig list
   (cheapest first) and keeps only rigs that are online, available, with
   `rpi >= min_rpi` (rigs with no history are skipped unless `allow_new_rigs`),
   whose last-30-min hashrate is at least `min_perf_ratio` of the advertised one,
   not in `excluded_regions` / `excluded_rigs`, and priced at most
   `max_price_premium` × the market's `last_10` price.
4. For each candidate, works out how many hours the budget buys (capped by the rig's
   max and `max_hours`) and the resulting **TH·hours**. The rig that buys the most
   TH·hours wins; among rigs within `price_tolerance` of the best, the **longest**
   rental wins (fewer gaps, fewer reconnects).
5. Rents with a price filter 1 % above the rig's rate (MRR rejects an exact match)
   and accounts for MRR's 3 % renter fee (`fee_rate`) so it never overspends.

Nothing is bought unless you pass `--live`. Every live action is appended to
`state.json` and, if configured, posted to Telegram.

## Dashboard: MRR Command Center

`dashboard.py` serves `www/index.html` plus a small JSON API on `dashboard_port`
(default 7160), protected by a password (cookie session, HTTP Basic also accepted).
A random password is written to `config.json` on first start if none is set.

It shows, refreshing every few seconds:

- **Gateway**: every stratum client on your DATUM gateway (address, worker, hashrate,
  shares, reject %, session age, user agent, rDNS), with MRR proxies and PyBLOCK
  rentals labelled, your own addresses highlighted (`own_addresses`) and a kick button.
  A 24 h hashrate history is kept in `history.json`.
- **Rentals**: active MRR rentals with what you paid for, what MRR says and what the
  gateway received, a verdict per rental (see [Rental audit](#rental-audit)), the
  address it mines to, time left, a link to the rental on MRR and one-click
  *quote / extend* for any number of hours.
- **Market**: the filtered rig list with what your budget buys, each rig's last 24 h of
  hashrate from MRR as a sparkline, its region with a flag, prices per TH·day and per
  hour, stars for favourite rigs, and a manual *rent* button (asks for confirmation and
  shows the estimated cost, fee included).
- **History**: finished rentals with their final verdict and cost, kept for
  `audit.history_days`.
- **Bot**: timer state, next/last run, enable/disable/run-now, the last 15 actions,
  and live editing of the strategy knobs (written back to `config.json`).
- **Logs**: journal of `mrr-bot.service` and of your DATUM gateway unit.

The gateway data comes from the DATUM gateway's own admin pages (`gateway.url`,
digest auth with `gateway.user` / `gateway.password`). The bot/timer controls use
`systemctl --user`, so run the dashboard as the same user that owns the units.

`launcher/mrr-command-center.sh` opens the dashboard as a standalone window
(Chromium app mode, own profile so the login cookie persists); `install.sh` adds
a desktop entry for it.

### Rental audit

MRR shows how much hashrate *it* measured for each rental. The audit checks that
against what your own gateway accepted, so you can tell a weak rig, a lossy path or
a wrong number apart. For every active rental the dashboard shows three figures:

| | source |
|---|---|
| **paid** | the rental's advertised TH/s |
| **MRR says** | MRR's own average for the rental |
| **gateway** | accepted shares × share difficulty for the rental's worker, from the gateway's client page |

Cards: **Cotejo** (totals), **Hashrate que llega por alquiler** (stacked chart per
rental, complete 10-minute buckets, with the paid total as a dashed line) and
**N alquileres** (expected vs received, the address it mines to, the three bars, a face
and the verdict, a link to the rental on MRR and its extend control).

The face is happy when the gateway receives more than both what was paid and what MRR
reports, straight within 5 % of what was paid and sad below that. The verdict next to it
is the statistical one.

How it measures (details in the docstring of `audit.py`):

- Every gateway poll is stored in `audit.db` (SQLite, `retention_days`, default 7).
- Each stratum connection has its own cumulative share counter and MRR's proxies
  reconnect often, so the work of a rental is the sum of the per-connection
  increments. A connection born inside the window counts from zero.
- Work is counted as **shares × vardiff**, not from the cumulative difficulty column:
  recent gateways print that column with an SI prefix and three significant digits
  (`2.10E`), and on a long-lived connection it does not move for hours.
- **Verdict**: the expected number of shares for the *paid* hashrate over the window
  is `TH/s × 1e12 × seconds / (vardiff × 2^32)`. A rental is `corto` (short) only when
  the shares received are more than `sigma` (default 3) standard deviations below that,
  `de sobra` (over) when above, `cumple` (ok) otherwise. Testing against the error of
  what was *measured* instead would call a rental that delivered 0.03 % "ok" (one share
  has 100 % error).
- Until 10 shares are expected the verdict is `arrancando` (starting).

**One worker per rental.** The gateway can only tell rentals apart by the stratum
worker (the part of the user after the first dot): all rentals arrive through MRR's
proxies, from a few IPs, and one rental can show several user agents. If your pool
profile gives every rental the same worker, those rentals are shown as
`worker compartido` and not judged. With `audit.assign_workers: true` the dashboard
fixes that itself: every active rental whose worker is shared (or missing) gets its
pool user rewritten through the MRR API to `<address>.<worker_prefix><rental id>`
(`PUT /rental/<id>/pool/<priority>`). That changes the pool of a rental you already
paid for (including the bot's rentals); it never rents or extends. Rigs reconnect
within a minute. Work from before the change cannot be split, so the rental is judged
from that moment on. It is **off by default**: turn it on once you have checked that the
worker name does not matter to your gateway or pool.

A rental made from the dashboard gets a worker of its own straight away: right after MRR
creates it, its pool user is rewritten to `<rent_address>.<worker_prefix><rental id>`.
`rent_address` is set from the market card (empty = the address of the pool profile), so
the payout address stays fixed while the worker changes with every rental. Rentals made
by the bot keep the profile's user.

Known limit: a connection that opens and closes between two polls (10 s) is not
counted, so the gateway figure can read slightly low for rigs that reconnect very often.

### History and favourites

When a rental stops being active, the audit judges it over its whole length (as far as
`retention_days` of samples go), reads its final price from MRR and keeps the result for
`history_days` (7): rig, end, length, paid / MRR / received TH/s, verdict and cost. A
rental that ends with an `ok` or `over` verdict at `favorite_pct` (95 %) or more of what
was paid stars its rig. Stars can also be set by hand in the market, which can be
filtered to show only them.

### Read-only mode and dashboard without the bot

`"read_only": true` hides the bot, rent and extend controls and makes the server refuse
`/api/bot`, `/api/config`, `/api/rent`, `/api/rent-address` and `/api/extend` (quotes
still work). Useful when the MRR key has rent permission but you only want to watch, or
the dashboard is shared.

`"bot_ui": false` takes the bot out of the dashboard: no timer switch, plan, parameters,
action history or bot log, and the server refuses `/api/bot` and `/api/config`. Nothing
rents on its own; the market keeps its **Rent** buttons and every rental waits for a
confirmation. Pair it with not enabling `mrr-bot.timer` if you only want to rent by hand.

### Price units

Market prices are shown per TH per day in hundred-millionths of MRR's currency. Its names
are configurable (`coin_label`, `unit_label`), and `alt_price` can add the same price in a
second unit: a JSON URL and the path of the value saying how much of that unit one MRR coin
buys (`invert` for the reverse). Extend is greyed out when the rig's owner does not allow
extensions or the rental is already at the rig's maximum length; pressing it shows MRR's
price for the extension and waits for a confirmation.

## Install

```bash
git clone https://github.com/MarcanoFilms/mrr-blake2b-bot ~/mrr-bot
cd ~/mrr-bot
./install.sh            # creates config.json from the example, installs the systemd user units + launcher
nano config.json        # api_key, api_secret, profile_id, gateway password, dashboard password
python3 -I mrr_bot.py status
python3 -I mrr_bot.py pools
python3 -I mrr_bot.py plan
systemctl --user enable --now mrr-dashboard.service
systemctl --user enable --now mrr-bot.timer   # only if you want the bot to spend on its own
```

On MRR you need an API key with *rentals* and *balance* permissions (and *rent* to rent
or extend), and a **pool profile** (Account → Pools) whose pool is your gateway's public
stratum address. `profile_id` is the number of that profile. `mrr_bot.py pools` resolves
the profile's host and checks the port is reachable.

If the gateway runs behind a VPN with port forwarding (the usual setup for a home
node), the stratum port MRR connects to is the forwarded one, not the local one.

## Config reference

Keys marked *market* decide which rigs the market card lists (and the bot rents); keys
marked *bot* only matter for the bot.

| key | default | meaning |
|---|---|---|
| `api_key`, `api_secret` | | MRR API credentials |
| `algo` | `blake2b` | MRR algo name |
| `currency` | `BTC` | balance / price currency |
| `profile_id` | | MRR pool profile that points at your gateway |
| `reserve_btc` | 0 | never spend below this confirmed balance (*bot*) |
| `max_spend_per_run_btc` | 0.002 | hard cap per pass (the timer interval is the cap window) (*bot*) |
| `max_concurrent_rentals` | 1 | rigs rented at the same time (*bot*) |
| `max_hours` / `min_hours` | 240 / 3 | rental length bounds (*market*) |
| `min_rpi` | 85 | rig performance index floor (0-100) (*market*) |
| `allow_new_rigs` | false | accept rigs with no rental history (*market*) |
| `min_perf_ratio` | 0.75 | last-30-min / advertised hashrate floor (*market*) |
| `max_price_premium` | 1.25 | ignore rigs above market `last_10` × this (*market*) |
| `price_tolerance` | 0.05 | ties within this fraction go to the longest rental (*bot*) |
| `fee_rate` | 0.03 | MRR renter fee added on top of the rig price |
| `extend_window_hours` | 2 | extend rentals ending within this window (*bot*) |
| `excluded_regions`, `excluded_rigs` | `[]` | blacklist by region code / rig id (*market*) |
| `telegram.token`, `telegram.chat_id` | | bot token + chat for action notifications |
| `dashboard_port` | 7160 | dashboard listen port (binds all interfaces) |
| `dashboard_password` | random | dashboard login |
| `own_addresses` | `[]` | your payout addresses, to tell your miners from foreign ones |
| `gateway.url/user/password` | `http://127.0.0.1:7158`, `oracle` | DATUM gateway admin page + digest auth |
| `datum_service` | `datum-convoy.service` | systemd unit whose journal the dashboard shows |
| `datum_service_scope` | `user` | `user` or `system` |
| `read_only` | false | the dashboard never spends nor touches the bot |
| `bot_ui` | true | false: no bot controls, history or log; renting only by hand |
| `rent_address` | `""` | payout address for rentals made from the dashboard, `""` = profile's |
| `coin_label` / `unit_label` | `BTC` / `sats` | names shown for MRR's currency and its 1e-8 unit |
| `alt_price` | `{}` | optional second price unit: `label`, `url`, `path`, `invert` |
| `audit.enabled` | true | record gateway samples and show the rental audit |
| `audit.assign_workers` | false | give each active rental its own worker through the MRR API |
| `audit.worker_prefix` | `mrr` | worker name = prefix + rental id |
| `audit.sigma` | 3 | standard deviations before calling a rental short |
| `audit.retention_days` | 7 | how long gateway samples are kept in `audit.db` |
| `audit.series_hours` / `audit.series_bucket_s` | 6 / 600 | per-rental chart span and bucket |
| `audit.history_days` | 7 | how long finished rentals stay in the history |
| `audit.favorite_pct` | 95 | % of the paid hashrate a finished rental needs to star its rig |

## Files

```
mrr_bot.py        the bot (CLI + MRR API client + strategy)
dashboard.py      MRR Command Center server
audit.py          rental audit: gateway samples, verdicts, per-rental worker, history, favourites
www/index.html    dashboard UI (single file, no build step)
www/img/          logo, favicon and login cover
docs/img/         screenshots for this README (personal data masked)
systemd/          mrr-bot.service, mrr-bot.timer, mrr-dashboard.service
launcher/         Chromium app-mode launcher + .desktop template
assets/           icon
config.json       your secrets and knobs (gitignored)
state.json        nonce + action log (gitignored)
history.json      gateway hashrate history (gitignored)
audit.db          gateway samples, rental history, favourites (gitignored)
```

## Notes and gotchas

- MRR API nonces must be strictly increasing per key. The bot and the dashboard
  share one key, so API calls are serialised with a file lock (`.mrr.lock`) and the
  last nonce lives in `state.json`. Do not run two copies against different state files.
- MRR applies a renter fee on top of the rig price (3 % at the time of writing). The
  bot assumes `fee_rate` when sizing rentals; the dashboard shows it in quotes.
- A rig's `rpi` can be the string `"new"`; those are skipped unless `allow_new_rigs`.
- IPv4-mapped client addresses (`::ffff:1.2.3.4`) are shown and reverse-resolved as
  IPv4. Clients whose worker is `<worker_prefix><digits>` are labelled `MRR proxy` even
  when the proxy has no reverse DNS.
- The dashboard UI text is in Spanish for now. Pull requests translating it are welcome.
- Run the scripts with `python3 -I` (as the units do) so nothing in the working
  directory can shadow standard modules.

## Credits

- Bot, MRR client and the MRR Command Center by [Marcano Trades](https://github.com/MarcanoFilms).
- Rental audit, one worker per rental, history and favourites, read-only / bot-less
  modes, market sparklines and flags, price units, the logo and the screenshots by
  [Kilombino](https://github.com/Kilombino), merged from his fork
  [Kilombino/mrr-command-center](https://github.com/Kilombino/mrr-command-center).

## License

MIT.
