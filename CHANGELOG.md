# Changelog

## 1.4.2 (21 Nov 2026)

- **Rent on P2Hash from the panel.** P2Hash is PyBLØCK's hashrate market on Nostr: sellers
  list rigs with the PyBLØCK Hashrate Agent (signed NIP-99 listings with a BOLT12 offer),
  the buyer asks by NIP-17 and pays each tranche (10 min, say) in advance over its own
  wallet connection (NWC). The panel speaks the agent's protocol (`pyblock-hashrate/0`) with
  a small standard-library implementation of what it needs (BIP-340 signatures, NIP-44 v2,
  gift wraps, WebSocket, NWC), checked against nostr-tools and run end to end against the
  agent's own seller code. A pink box in the market card lists the rigs; a rental shows in
  the rentals card next to MRR's and PyBLØCK's with the same face, verdict and bars, the
  tranches paid with what the gateway measured in each, a countdown for the next payment
  and a Stop button; finished ones go to the history.
- **Nothing pays on its own by default.** Every tranche waits for a click ("Pagar tramo
  N", with the seller's grace period counting down and a Telegram notice). Paying tranches
  automatically needs the switch on, both caps set (sats per rental and per 24 h, 0 by
  default) and the box ticked for that rental. Either way a tranche is paid only if it is
  the next one of the running rental, for the listing's offer and price (+1 %), within the
  hours bought and the caps, and (from the 2nd on) the gateway saw at least half the
  advertised hash since the last payment, measured on the rental's own worker
  (`<address>.p2h<6 hex>`). The paying connection is pasted in the box, kept only in
  `config.json` (600) and never shown back; its budget is read before renting.
- **PyBLØCK market rentals made on their web**: "Añadir orden de PyBLØCK" takes the order's
  secret link (`market_minero.php?o=…`, read through `GET /orders/{secret}`) and follows it
  whole: rigs, the stratum user it mines with (so it is measured on its worker), status,
  refund. With only the miner's public id (`m·25f278f0`) the rig's current rental is
  followed as an estimate (verified TH/s, end and list price from `/rigs`), measured by
  address.
- **Detected connections without a worker are measured**: the gateway samples of a rental
  connection that sends only an address are stored under `@<source>:<address>`, so they get
  a real TH/s (since they arrived, or over the last hour) instead of "–", and PyBLØCK
  orders followed by address use the same key.

## 1.4.1 (20 Nov 2026)

- **The automatic ticket now follows MRR's policy**: it looks at a rental only after it has
  ended, inside MRR's 12-hour window, 8.5 h after the end (MRR reviews short rentals itself
  and refunds at the end or up to ~8.7 h later), and flags it when the final delivery is
  below 100 % beyond statistical noise (3 sigmas of the expected shares). Rentals MRR has
  already refunded are skipped (`was_refunded` or a Credit/Refund in
  `/account/transactions`). The Kilobot notice carries the ticket text and the time left;
  posting the evidence to the owner is a separate box (off).
- The mid-rental part is now only an optional early, polite message to the owner on the
  rental's thread (off by default), as MRR asks.
- **Your own miners are not rentals**: `own_workers` / `own_user_agents`, an "es mío" button
  on detected rows, and a separate "tu minero (sin alquilar)" line (a rig listed on
  PyBLØCK market comes back to your gateway through PyBLØCK's proxy when not rented).
- The history's "Abrir ticket" uses the same noise test, shows "reembolsado" / "avisado",
  and the ticket dialog says when MRR has already refunded the rental.

## 1.4.0 (20 Nov 2026)

- **Tickets for rentals that underdeliver.** MRR's API has no endpoint for tickets,
  disputes or refunds, so every MRR rental with the sad face gets an "Abrir ticket" button:
  the evidence (paid, what MRR says, what reached the gateway, shares against expected)
  ready to paste, copied, and the rental opened on MRR. Finished rentals keep the button
  while MRR's 12-hour claim window is open, with a countdown.
- **Automatic notice** (switch above the rentals, off by default): once per rental, while
  it runs with the rig online, past a quarter of its length and clearly short, the evidence
  is stored, a Telegram notice with the link is sent and, if a second box is ticked, the
  evidence is posted on the rental's message thread for the owner (the only write MRR's API
  allows here).
- **History**: a delete button on every entry (with a confirmation); paid PyBLØCK orders go
  to the history too; a rental MRR's list left out once is no longer taken for finished
  while it still runs (such entries are removed when it shows up again).
- **Other markets**: hashrate from PyBLØCK market and from configurable sources (p2phash,
  TideHash, RouteHash by default; matched on worker, user agent, reverse DNS or stratum
  user) is listed as "detected" with its TH/s now and over the last hour.
- Fixes: rig names MRR sends HTML-escaped are no longer escaped twice; the history no
  longer shows "BTC" instead of the configured coin name on first load; the extend box of
  a rental already past the rig's maximum no longer shows a negative number; renewal
  prices survive MRR's 100 calls/min limit (the last known ones are kept).
- Market: the rigs the filters drop are listed greyed with the reason under the usable
  ones, rentable all the same.

## 1.3.0 (15 Nov 2026)

- PyBLØCK orders made outside the panel can be added by pasting the order id or tracking
  link, measured on their worker or, without one, by payout address as an estimate.
- Unpaid PyBLØCK invoices are shown as such instead of as an expired rental.
- Extend: the cost is estimated from the rig's current price (MRR counted `getcost` as an
  extension request and refused the real one as too soon) and MRR's refusal is shown.

## 1.2.0 (15 Nov 2026)

- PyBLØCK rentals next to MRR: packages, manual orders with an invoice paid from your own
  wallet, status, and what each order delivers to the gateway.
- Price per TH/day paid and renewal price on every rental; faces, stars and verdicts for
  PyBLØCK rentals.

## 1.1.0 (13 Nov 2026)

- MRR balance as a pill in the header; minutes when less than an hour is left; favourite
  star under each rental's face; market columns RPI, Perf 24 h and Perf 30 min coloured.

## 1.0.0 (6 Nov 2026)

- First release: rental audit, manual renting, history and favourites, packaged for
  Umbrel and StartOS.
