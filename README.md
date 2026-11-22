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

- **Every rental judged**: what you paid for, what MRR says it delivered and what your
  gateway accepted, with a statistical verdict and a face that tells you at a glance.
- **One worker per rental**, so rentals that all arrive through MRR's proxies can be told
  apart, mining to a payout address you fix.
- **Manual renting** from the market, with each rig's last 24 h of hashrate, its region,
  prices per TH and day and per hour, and a confirmation before anything is spent.
- **History and favourites**: finished rentals (MRR and PyBLØCK) are kept for 7 days with
  their final verdict, can be deleted by hand, and rigs that delivered are starred
  automatically.
- **Tickets**: a rental that underdelivers gets an "Abrir ticket" button with the evidence
  (paid vs MRR vs gateway) ready to paste, and an optional automatic notice.
- **Other markets**: PyBLØCK packages and orders (also PyBLØCK market rentals made on
  their web), renting on **P2Hash** (PyBLØCK's market on Nostr, paid per tranche only while
  the hash arrives), and hashrate from other markets detected on the gateway.
- **Gateway**: every ASIC connected to your DATUM gateway (address, worker, hashrate,
  shares, kick button) and the gateway's log.

| Rentals | History |
|---|---|
| ![Rentals](docs/img/rentals.jpg) | ![History](docs/img/history.jpg) |

![Market](docs/img/market.jpg)

![P2Hash](docs/img/p2hash.jpg)

| A P2Hash rental, a tranche waiting for the click | A detected rental without a worker, measured by address |
|---|---|
| ![P2Hash rental](docs/img/p2hash-rental.jpg) | ![Detected](docs/img/detected.jpg) |

<sub>The P2Hash pictures show sample data (no P2Hash seller was listed when they were taken).</sub>

<p align="center"><img src="docs/img/ticket.jpg" width="540" alt="Ticket text"> <img src="docs/img/login.jpg" width="420" alt="Login"></p>

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

### PyBLØCK rentals

Next to MRR the market card lists the packages of the
[PyBLØCK Market](https://b.pyblock.xyz:8443/api/market/v1.php/openapi.json) (public API v1,
`/rentals/packages`, cached 2 min) in its own purple box: TH/s, hours, units (each step
has its own price), total, price per TH per day and how it compares with MRR's market
(`last_10` plus MRR's fee, same unit). The promo, if any, is shown as a badge. A
**Probar gateway** button runs PyBLØCK's own gateway check (`/payout/gateway-test`)
without creating anything.

Renting is manual, like MRR: choose units and pay method (`ln`, `xbtln`, `xbt`), confirm
(total, TH/s, hours, gateway, payout address), and the dashboard sends the order with a
random `client_nonce` that it keeps, so a retry after a timeout or a rate limit is the same
order. PyBLØCK checks the gateway first; its refusal is shown as is. The answer is an
invoice (or an on-chain address and exact amount) shown with a QR code and its
countdown: **you pay it from your own wallet**, the dashboard never pays. Its status is
then read every 15 s while unpaid and every minute while it runs.

The gateway user is `<address>.<pyblock.worker_prefix><6 hex>`, so the order has a worker
of its own in the gateway table (labelled `PyBLOCK rental`). The address is
`pyblock.address`, else `rent_address`, else the MRR profile's.

The order id PyBLØCK returns **is the order's secret**. It is kept only in `pyblock.json`
(mode 600, gitignored), never logged, and never returned by the dashboard's API: orders
are referred to by a local random `ref`, and "ver en PyBLØCK" goes through a redirect.
PyBLØCK orders appear in the rentals card with a purple badge (contracted TH/s, what
PyBLØCK reports, what the gateway sees for its worker, time left, state), in the rented
TH/s totals and in `/api/overview` under `pyblock`. Created / paid / ended notices go to
the same Telegram chat, without the order id or the invoice.

### PyBLØCK market rentals made on their web

PyBLØCK market (rigs listed by miners) is not the packages above: an order there is made
on their web and identified by a secret. Paste its link (`…/market_minero.php?o=<secret>`)
or the secret in **"Añadir orden de PyBLØCK"** and the panel reads `GET /orders/{secret}`:
rigs, hours, total, the stratum user it mines with (so it is measured on that worker),
status (`esperando_pago`, `activo`, `liquidado`, `vencido`) and the refund once settled.
The secret stays in `pyblock.json`. Pasting only the miner's public id (`m·25f278f0`, shown
next to each rig) follows that rig's current rental as an **estimate**: verified TH/s, the
end and the list price from `/rigs`, measured by address.

### P2Hash: renting per tranche over Nostr

[P2Hash](https://b.pyblock.xyz:8443/p2hash.php) is PyBLØCK's peer-to-peer hashrate market:
sellers run the [PyBLØCK Hashrate Agent](https://gitlab.com/astrolexis-group1/pyblock-hashrate-agent)
next to the rig. The panel is a buyer that speaks the agent's protocol (`pyblock-hashrate/0`):

1. listings are signed Nostr events (NIP-99, kind 30402) read from `p2hash.relays`; only
   valid signatures, not expired, with a BOLT12 offer, a price in XBT/TH/day and a tranche
   of at least 60 s are shown (pink box in the market card), with the price against MRR's;
2. **Alquilar** (hours, confirmation) sends an encrypted request (NIP-17 gift wrap, its own
   buyer key in `p2hash.json`) naming your public gateway and a worker of its own,
   `<address>.p2h<6 hex>`; the seller checks the gateway and accepts;
3. the seller asks for each tranche in advance (`due`); the panel pays the listing's offer
   over the paying wallet connection (NWC `pay_offer`, or `pay` for wallets that use it)
   and sends the preimage; the seller checks it in its wallet and sends the hash;
4. the rental shows in the rentals card: the same face, verdict and bars as MRR's (gateway
   measured by shares on its worker), the tranches paid with what the gateway saw in each
   and what the seller reported, time left, next payment and a **Parar** button (tells the
   seller, pays nothing more). Finished rentals go to the history.

**Payments.** Nothing is paid unless you ask for it:

- by default every tranche waits for your click (**Pagar tramo N**) within the seller's
  grace period (a couple of minutes, counted down), and Kilobot tells you one is due; if
  nobody pays, the seller sends the rig home;
- paying tranches by themselves needs three things: the **pago automático** switch on, both
  **caps** set (sats per rental and per 24 h; 0 = off, the default) and the box ticked when
  renting. Switching it off during a rental makes the next tranche wait for you;
- in both modes a tranche is paid only if it comes from the seller of the running rental,
  is the next in order and was never paid, is for the listing's offer, asks no more than
  the listing price (+1 %), stays within the hours bought and the caps, and, from the
  second one on, the gateway measured at least `p2hash.min_pct` (50 %) of the advertised
  hash since the previous payment. Otherwise it is not paid and the rental is cancelled:
  your risk is one tranche;
- a wallet that does not answer in time counts against the caps anyway (it may have paid);
- the **paying connection** should be a separate app in your hub with *pay* and a budget:
  the hub enforces the budget, and the panel reads it (`get_budget`, or `get_balance`)
  and refuses a rental it could not finish paying. Paste its `nostr+walletconnect://` URI
  in the box: it is written to `config.json` (600) and never shown again.

A panel restart closes a running rental and tells the seller (it is not resumed).
**Probar gateway** makes the seller's own check (stratum subscribe + authorize on your
public host). Notices (accepted, tranche due, first payment, end) go to the Telegram chat.

### Tickets for rentals that underdeliver

MRR's API v2 has **no endpoint to open a support ticket, a dispute or a refund request**
(checked against [the API docs](https://www.miningrigrentals.com/apidocv2) on 20 Nov 2026).
Tickets are opened on MRR's site, from the rental's page or the ticket centre, and MRR's
[refund policy](https://www.miningrigrentals.com/helpcenter/For-Renters/35) reviews partial
delivery once the rental has ended, within 12 hours of the end; during a rental it asks
renters to contact the rig owner first. What the API does have is the rental's message
thread (`PUT /rental/<id>/message`), which the owner and MRR support read.

So the dashboard does this:

- **"Abrir ticket"** on every active rental with the sad face (below 95 % of what was paid,
  3 sigmas short) and on finished ones while MRR's 12-hour window is open (the button
  counts it down). It shows the evidence in English (advertised TH/s, what MRR reports,
  what the gateway accepted from the rental's own worker, shares against expected,
  window), copies it and opens the rental on MRR, where its ticket button is.
- **"ticket automático al terminar"** (off by default, a switch above the rentals) follows
  MRR's policy: it looks at an MRR rental only once it has **ended**, inside the 12-hour
  window, `audit.tickets.after_end_h` (8.5 h) after the end, because MRR reviews short
  rentals itself and its automatic refunds arrive at the end or 7.5–8.7 hours later. It
  flags the rental when the final delivery is below `max_pct` (100 %) **beyond statistical
  noise**: shares arrive as a Poisson process, so a rig that delivered exactly what was paid
  is measured within about 1/√(expected shares); the notice needs the shortfall to exceed
  `sigma` (3) of those deviations (a rig that delivered in full is wrongly flagged about
  once in 700 rentals) and at least `min_expected_shares`. It skips rentals MRR already
  refunded (the rental's `was_refunded`, or a `Credit/Refund` for it in
  `/account/transactions`). Then, once per rental, the evidence is stored in `audit.db`
  (table `tickets`) and Kilobot sends the ticket text with the time left to claim. A
  second box, **"publicar las pruebas al dueño"** (off), also posts it on the rental's
  message thread.
- **"avisar antes al dueño a medio alquiler"** (off): MRR asks renters to contact the owner
  first, so a running rental that is clearly short (past `min_fraction`, rig online, below
  `early_max_pct` 95 %, 3 sigmas) gets one polite message on its thread with the figures.
- Nothing is sent in `read_only`. The ticket dialog also tells you when MRR has already
  refunded the rental.

### Rentals from other markets

Hashrate that reaches the gateway from a rental market without an order in the panel is
listed in the rentals card as **detected** (orange or purple badge): source, worker, masked
address, user agent, TH/s now and over the last hour by shares (or since it arrived, when
that is less than an hour ago), and since when it is seen. A connection that sends only an
address (no worker) is measured too: its samples are stored under `@<source>:<address>`, so
it is told apart by the market it comes from (label from reverse DNS or user agent). It has
no contract, so no face or verdict. Sources:

- **PyBLØCK market**: connections from PyBLØCK's proxy (user agent `pyblock-market/…` or its
  reverse DNS) that are not one of the PyBLØCK orders here.
- **Your own miners are never rentals**: `own_workers` (`"worker"` or `"address.worker"`)
  and `own_user_agents` in `config.json`, or the **"es mío"** button on a detected row. A rig
  you list on PyBLØCK market comes back to your gateway through its proxy when nobody rents
  it; it is shown apart as "tu minero (sin alquilar)" and can be un-marked there.
- `other_sources` in `config.json`: a name and substrings matched against the worker, user
  agent, reverse DNS and stratum user. Defaults: `p2phash`, `tidehash`, `routehash`.

Paid PyBLØCK orders that end now go to the history too, judged on their worker.

### History and favourites

When a rental stops being active, the audit judges it over its whole length (as far as
`retention_days` of samples go), reads its final price from MRR and keeps the result for
`history_days` (7): rig, end, length, paid / MRR / received TH/s, verdict and cost. A
rental that ends with an `ok` or `over` verdict at `favorite_pct` (95 %) or more of what
was paid stars its rig. Stars can also be set by hand in the market, which can be
filtered to show only them. Each entry has a 🗑 button to delete it now (with a
confirmation) instead of waiting for `history_days`. A rental that MRR's list leaves out
once (a glitch or its 100 calls/min limit) is no longer taken for finished while it runs.

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
| `pyblock.enabled` | true | show PyBLØCK packages and allow manual orders |
| `pyblock.api` | `https://b.pyblock.xyz:8443/api/market/v1.php` | PyBLØCK Market API base |
| `pyblock.gateway.host/port` | | public stratum of your DATUM gateway (required to order) |
| `pyblock.backup.host/port` | | optional second gateway |
| `pyblock.address` | `""` | payout address for PyBLØCK orders, `""` = `rent_address`, then the profile's |
| `pyblock.worker_prefix` | `pyb` | worker = prefix + 6 random hex per order |
| `p2hash.enabled` | true | show P2Hash listings and allow renting |
| `p2hash.relays` | PyBLØCK's, relay.kilombino.com, nos.lol | Nostr relays for listings and messages |
| `p2hash.nwc` | `""` | the paying wallet connection (set from the dashboard; never shown) |
| `p2hash.gateway.host/port` | `pyblock.gateway` | public stratum the seller's rig mines to |
| `p2hash.caps.rental_sats` / `day_sats` | 0 / 0 | caps (0 = off; automatic payment needs both; the dashboard's values win) |
| `p2hash.min_pct` | 0.5 | least share of the advertised hash, since the last payment, that keeps paying |
| `p2hash.worker_prefix` | `p2h` | worker = prefix + 6 random hex per rental |
| `audit.enabled` | true | record gateway samples and show the rental audit |
| `audit.assign_workers` | false | give each active rental its own worker through the MRR API |
| `audit.worker_prefix` | `mrr` | worker name = prefix + rental id |
| `audit.sigma` | 3 | standard deviations before calling a rental short |
| `audit.retention_days` | 7 | how long gateway samples are kept in `audit.db` |
| `audit.series_hours` / `audit.series_bucket_s` | 6 / 600 | per-rental chart span and bucket |
| `audit.history_days` | 7 | how long finished rentals stay in the history |
| `audit.favorite_pct` | 95 | % of the paid hashrate a finished rental needs to star its rig |
| `audit.tickets.max_pct` | 100 | after the end: flag only below this % of what was paid (and beyond `sigma`) |
| `audit.tickets.sigma` | `audit.sigma` (3) | standard deviations of shortfall needed after the end |
| `audit.tickets.min_expected_shares` | 200 | shares the paid hashrate should have brought in, to judge at all |
| `audit.tickets.after_end_h` / `window_h` | 8.5 / 12 | when to look after the end / MRR's claim window |
| `audit.tickets.min_fraction` / `min_observed_h` / `early_max_pct` | 0.25 / 1 / 95 | the early warning to the owner |
| `own_workers` / `own_user_agents` | `[]` | your own miners, never shown as rentals |
| `other_sources` | p2phash, TideHash, RouteHash | `[{"name": …, "match": [substrings]}]` for detected rentals |

## Files

```
mrr_bot.py        the bot (CLI + MRR API client + strategy)
dashboard.py      MRR Command Center server
pyblock.py        PyBLØCK Market: packages, manual orders, market orders, order status
p2hash.py         P2Hash renter: listings, requests, tranche payments, caps
nostr.py          minimal Nostr in the standard library (BIP-340, NIP-44, NIP-17, NWC, WebSocket)
tickets.py        evidence text for MRR tickets and the automatic notice
tests/            unit tests (python3 -m unittest discover -s tests)
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
audit.db          gateway samples, rental history, favourites, ticket log (gitignored)
pyblock.json      PyBLØCK orders with their secret ids (600, gitignored)
p2hash.json       P2Hash buyer key, payments (24 h cap) and history (600, gitignored)
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
