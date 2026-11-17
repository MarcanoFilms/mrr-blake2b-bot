#!/usr/bin/env python3
"""MRR rental bot: buy as many Blake2B TH-hours as the account budget allows,
preferring long rentals, pointed at the DATUM gateway profile.

Commands:
  market   show the cheapest usable rigs and what the budget buys
  status   balance, active rentals, time remaining
  plan     dry run: what the bot would do right now (no spending)
  run      same as plan; spends only with --live
  pools    show the profile's pools and check the gateway host/port

Spending is impossible without --live. Every live action is logged to
state.json and sent to Telegram when configured.
"""
import argparse
import fcntl
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOCK_PATH = os.path.join(HERE, ".mrr.lock")   # serialises API calls across processes (nonces must arrive in order)
BASE = "https://www.miningrigrentals.com/api/v2"

DEFAULTS = {
    "algo": "blake2b",
    "currency": "BTC",
    "profile_id": None,            # MRR pool profile that points at the gateway
    "reserve_btc": 0.0,            # never spend below this confirmed balance
    "max_spend_per_run_btc": 0.002,  # hard cap per invocation (timer interval = cap window)
    "max_concurrent_rentals": 1,   # how many rigs at once
    "max_hours": 240,              # never rent a single rig longer than this
    "min_hours": 3,                # don't bother renting shorter than this
    "min_rpi": 85,                 # rig performance index floor (0-100)
    "allow_new_rigs": False,       # rigs with rpi "new" (no history)
    "min_perf_ratio": 0.75,        # last_30min / advertised, when the rig reports hashrate
    "max_price_premium": 1.25,     # ignore rigs pricier than market last_10 * this
    "price_tolerance": 0.05,       # among rigs within 5% of best TH-h, take the longest
    "fee_rate": 0.03,              # MRR adds this on top of the rig price (verified: need/have = 1.03)
    "extend_window_hours": 2.0,    # extend a rental that ends within this window
    "excluded_regions": [],
    "excluded_rigs": [],
    "telegram": {"token": "", "chat_id": ""},
}

UNIT = {"hash": 1e-12, "kh": 1e-9, "mh": 1e-6, "gh": 1e-3, "th": 1.0, "ph": 1e3, "eh": 1e6}


# ----------------------------------------------------------------------------- helpers
def load_config():
    with open(CONFIG_PATH) as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    tg = dict(DEFAULTS["telegram"])
    tg.update(raw.get("telegram") or {})
    cfg["telegram"] = tg
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_nonce": 0, "actions": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def to_th(hashobj):
    """{'hash': x, 'type': 'mh'} -> TH/s (float)."""
    if not hashobj:
        return 0.0
    try:
        return float(hashobj.get("hash") or 0) * UNIT.get((hashobj.get("type") or "th").lower(), 1.0)
    except (TypeError, ValueError):
        return 0.0


def fmt_btc(x):
    return f"{x:.8f}"


def now_utc():
    return datetime.now(timezone.utc)


def parse_mrr_time(s):
    # "2017-12-28 19:21:04 UTC"
    return datetime.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


class MRR:
    def __init__(self, cfg, state):
        self.key = cfg["api_key"]
        self.secret = cfg["api_secret"]
        self.state = state

    def _nonce(self):
        n = int(time.time() * 10000)
        last = int(self.state.get("last_nonce") or 0)
        if n <= last:
            n = last + 1
        self.state["last_nonce"] = n
        return str(n)

    def call(self, method, endpoint, params=None, body=None):
        with open(LOCK_PATH, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                return self._call(method, endpoint, params, body)
            except RuntimeError as e:
                if "Bad Nonce" not in str(e):
                    raise
                time.sleep(0.2)
                return self._call(method, endpoint, params, body)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _call(self, method, endpoint, params=None, body=None):
        nonce = self._nonce()
        sig = hmac.new(self.secret.encode(), (self.key + nonce + endpoint).encode(), hashlib.sha1).hexdigest()
        url = BASE + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "x-api-key": self.key, "x-api-sign": sig, "x-api-nonce": nonce,
            "Content-Type": "application/json", "User-Agent": "mrr-bot/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} on {method} {endpoint}: {e.read()[:300]!r}")
        if not payload.get("success"):
            raise RuntimeError(f"MRR error on {method} {endpoint}: {json.dumps(payload.get('data'))[:400]}")
        return payload["data"]

    # read
    def balance(self, cur):
        b = self.call("GET", "/account/balance")[cur]
        return float(b["confirmed"]), float(b["unconfirmed"])

    def algo_stats(self, algo):
        for a in self.call("GET", "/info/algos"):
            if a["name"] == algo:
                return a
        raise RuntimeError(f"algo {algo} not found")

    def rigs(self, algo, pages=3):
        out = []
        for i in range(pages):
            d = self.call("GET", "/rig", {"type": algo, "count": 100, "offset": i * 100,
                                          "orderby": "price", "orderdir": "asc"})
            out.extend(d["records"])
            if len(d["records"]) < 100:
                break
        return out

    def active_rentals(self):
        return self.call("GET", "/rental", {"type": "renter", "history": "false", "limit": 50})["rentals"]

    def rental(self, rid):
        return self.call("GET", f"/rental/{rid}")

    def profile(self, pid):
        return self.call("GET", f"/account/profile/{pid}")

    # write
    def extend_cost(self, rid, hours):
        return self.call("PUT", f"/rental/{rid}/extend", body={"length": hours, "getcost": 1})

    def extend(self, rid, hours):
        return self.call("PUT", f"/rental/{rid}/extend", body={"length": hours})

    def rent(self, rig_id, hours, profile_id, currency, price_type, price):
        return self.call("PUT", "/rental", body={
            "rig": int(rig_id), "length": round(hours, 2), "profile": int(profile_id),
            "currency": currency,
            # filter must be >= the rig's price; MRR rejects an exact match, so allow 1% headroom
            "rate": {"type": price_type, "price": f"{price * 1.01:.8f}"},
        })


def telegram(cfg, text):
    tg = cfg["telegram"]
    if not tg.get("token") or not tg.get("chat_id"):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tg['token']}/sendMessage", data=data, timeout=15)
    except Exception as e:  # never let alerts break the bot
        print(f"[telegram failed: {e}]", file=sys.stderr)


# ----------------------------------------------------------------------------- analysis
def candidate_rigs(cfg, rigs, market_price, rejected=None):
    """Usable rigs, cheapest first. If [rejected] is a list, the available+online rigs that a
    filter drops are appended to it with the reason (the dashboard shows them apart, 17 nov 2026)."""
    cur = cfg["currency"]
    out = []
    for r in rigs:
        p = (r.get("price") or {}).get(cur) or {}
        st = r.get("status") or {}
        if not p.get("enabled") or st.get("status") != "available" or not r.get("online"):
            continue   # not rentable at all: never listed
        info = {"id": str(r["id"]), "name": r["name"], "region": r.get("region")}
        try:
            rpi = float(r.get("rpi") or 0)
        except (TypeError, ValueError):   # "new" = no rental history yet
            rpi = -1.0
        info["rpi"] = rpi
        adv = to_th(r["hashrate"]["advertised"])
        price = float(p.get("price") or 0)              # per unit per day
        unit = (r["price"].get("type") or "th").lower()
        price_th_day = price / UNIT.get(unit, 1.0)      # normalise to BTC per TH per day
        hour_cost = float(p["hour"]) if p.get("hour") else (price * float(r["hashrate"]["advertised"]["hash"]) / 24 if adv > 0 else 0)
        recent = to_th(r["hashrate"].get("last_30min"))
        perf = recent / adv if recent > 0 and adv > 0 else None
        minh = max(float(r.get("minhours") or 0), float(p.get("min_rental_length") or 0), cfg["min_hours"])
        maxh = min(float(r.get("maxhours") or cfg["max_hours"]), cfg["max_hours"])
        info.update({"adv_th": adv, "perf": perf, "price_th_day": price_th_day, "price_raw": price,
                     "price_type": unit, "hour_cost": hour_cost, "minh": minh, "maxh": maxh})
        reason = None
        if str(r["id"]) in map(str, cfg["excluded_rigs"]) or r.get("region") in cfg["excluded_regions"]:
            reason = "excluido en la configuración"
        elif rpi < 0 and not cfg.get("allow_new_rigs"):
            reason = "rig nuevo (sin historial de RPI)"
        elif 0 <= rpi < cfg["min_rpi"]:
            reason = f"RPI {rpi:.1f} < {cfg['min_rpi']:g}"
        elif adv <= 0:
            reason = "sin hashrate anunciado"
        elif market_price and price_th_day > market_price * cfg["max_price_premium"]:
            reason = f"caro: {price_th_day / market_price:.2f}× el mercado (máx. {cfg['max_price_premium']:g}×)"
        elif hour_cost <= 0:
            reason = "sin precio por hora"
        elif perf is not None and perf < cfg["min_perf_ratio"]:
            reason = f"rinde {perf * 100:.0f}% de lo anunciado (mín. {cfg['min_perf_ratio'] * 100:.0f}%)"
        elif maxh < minh:
            reason = f"horas incompatibles ({minh:g}–{maxh:g} h)"
        if reason:
            if rejected is not None and adv > 0 and hour_cost > 0:
                rejected.append(dict(info, reason=reason))
            continue
        out.append(info)
    out.sort(key=lambda c: (c["price_th_day"], -c["maxh"], -c["rpi"]))
    if rejected is not None:
        rejected.sort(key=lambda c: c["price_th_day"])
    return out


def pick(cfg, cands, budget):
    """Choose the rig+hours that buys the most TH-hours; among near-ties take the longest."""
    options = []
    fee = 1 + cfg["fee_rate"]
    for c in cands:
        hours = min(c["maxh"], budget / (c["hour_cost"] * fee))
        hours = int(hours * 100) / 100
        if hours < c["minh"]:
            continue
        options.append((c, hours, hours * c["adv_th"], hours * c["hour_cost"] * fee))
    if not options:
        return None
    best_thh = max(o[2] for o in options)
    near = [o for o in options if o[2] >= best_thh * (1 - cfg["price_tolerance"])]
    near.sort(key=lambda o: (-o[1], -o[2]))
    return near[0]


def rental_remaining_hours(rental):
    end = parse_mrr_time(rental["end"])
    return (end - now_utc()).total_seconds() / 3600


def decide(cfg, api, verbose=True):
    """Return (actions, context). Never spends."""
    cur = cfg["currency"]
    confirmed, unconfirmed = api.balance(cur)
    stats = api.algo_stats(cfg["algo"])
    market = float(stats["stats"]["prices"]["last_10"]["amount"])   # BTC per TH*day
    rentals = api.active_rentals()
    rentals = [r for r in rentals if rental_remaining_hours(r) > 0]
    budget = max(0.0, min(confirmed - cfg["reserve_btc"], cfg["max_spend_per_run_btc"]))
    actions = []

    if verbose:
        print(f"balance {cur}: confirmed {fmt_btc(confirmed)}  unconfirmed {fmt_btc(unconfirmed)}  "
              f"reserve {fmt_btc(cfg['reserve_btc'])}  -> budget this run {fmt_btc(budget)}")
        print(f"market {cfg['algo']}: last_10 {fmt_btc(market)} BTC/TH/day, "
              f"lowest {stats['stats']['prices']['lowest']['amount']}, "
              f"{stats['stats']['available']['rigs']} rigs / {stats['stats']['available']['hash']['nice']} available")
        for r in rentals:
            print(f"active rental {r['id']}: rig {r['rig']['id']} {r['rig']['name']} "
                  f"{r['hashrate']['advertised']['nice']} ends {r['end']} ({rental_remaining_hours(r):.1f} h left)")

    # 1) extend rentals that are about to end (continuity beats a new rental)
    for r in rentals:
        left = rental_remaining_hours(r)
        if left > cfg["extend_window_hours"]:
            continue
        hour_cost = float(r["price"]["paid"]) / float(r["length"]) if float(r["length"]) else 0
        if hour_cost <= 0:
            continue
        rig_max = float(r["rig"].get("maxhours") or cfg["max_hours"])
        hours = min(cfg["max_hours"], rig_max, budget / (hour_cost * (1 + cfg["fee_rate"])))
        hours = int(hours * 100) / 100
        if hours < cfg["min_hours"]:
            continue
        try:
            q = api.extend_cost(r["id"], hours)
            cost = float(q.get("cost") or 0)
        except RuntimeError as e:
            if verbose:
                print(f"extend quote failed for {r['id']}: {e}")
            continue
        if 0 < cost <= budget:
            actions.append({"type": "extend", "rental": r["id"], "rig": r["rig"]["id"], "name": r["rig"]["name"],
                            "hours": hours, "cost": cost})
            budget -= cost

    # 2) new rentals up to max_concurrent
    slots = cfg["max_concurrent_rentals"] - len(rentals)
    if slots > 0:
        cands = candidate_rigs(cfg, api.rigs(cfg["algo"]), market)
        if verbose:
            print(f"{len(cands)} usable rigs after filters (rpi>={cfg['min_rpi']}, "
                  f"price<= {cfg['max_price_premium']}x market)")
        taken = set()
        for _ in range(slots):
            choice = pick(cfg, [c for c in cands if c["id"] not in taken], budget)
            if not choice:
                break
            c, hours, thh, cost = choice
            taken.add(c["id"])
            actions.append({"type": "rent", "rig": c["id"], "name": c["name"], "region": c["region"],
                            "adv_th": c["adv_th"], "rpi": c["rpi"], "hours": hours, "cost": cost,
                            "th_hours": thh, "price_raw": c["price_raw"], "price_type": c["price_type"],
                            "price_th_day": c["price_th_day"]})
            budget -= cost

    ctx = {"confirmed": confirmed, "unconfirmed": unconfirmed, "market": market, "rentals": rentals,
           "budget_left": budget}
    return actions, ctx


def describe(a):
    if a["type"] == "rent":
        return (f"RENT rig {a['rig']} \"{a['name']}\" [{a['region']}] {a['adv_th']:.2f} TH rpi {a['rpi']:.0f} "
                f"for {a['hours']:.2f} h = {a['th_hours']:.0f} TH-h, cost {fmt_btc(a['cost'])} BTC "
                f"({fmt_btc(a['price_th_day'])}/TH/day)")
    return (f"EXTEND rental {a['rental']} rig {a['rig']} \"{a['name']}\" by {a['hours']:.2f} h, "
            f"cost {fmt_btc(a['cost'])} BTC")


# ----------------------------------------------------------------------------- commands
def cmd_market(cfg, api, args):
    cur = cfg["currency"]
    confirmed, unconfirmed = api.balance(cur)
    stats = api.algo_stats(cfg["algo"])
    market = float(stats["stats"]["prices"]["last_10"]["amount"])
    cands = candidate_rigs(cfg, api.rigs(cfg["algo"]), market)
    budget = args.budget if args.budget is not None else max(0.0, confirmed - cfg["reserve_btc"])
    print(f"confirmed {fmt_btc(confirmed)} BTC (unconfirmed {fmt_btc(unconfirmed)}), "
          f"market last_10 {fmt_btc(market)} BTC/TH/day, budget considered {fmt_btc(budget)}")
    print(f"{'rig':>7} {'name':<28} {'reg':<6} {'TH':>6} {'rpi':>5} {'perf':>5} {'BTC/TH/d':>11} "
          f"{'BTC/h':>12} {'min-max h':>10} {'afford h':>9} {'TH-h':>8}")
    fee = 1 + cfg["fee_rate"]
    for c in cands[: args.top]:
        hours = min(c["maxh"], budget / (c["hour_cost"] * fee)) if budget > 0 else 0
        ok = "" if hours >= c["minh"] else " (short)"
        perf = f"{c['perf']*100:.0f}%" if c["perf"] is not None else "n/a"
        print(f"{c['id']:>7} {c['name'][:28]:<28} {str(c['region']):<6} {c['adv_th']:>6.2f} {c['rpi']:>5.0f} "
              f"{perf:>5} {c['price_th_day']:>11.8f} {c['hour_cost']:>12.9f} "
              f"{c['minh']:>4.0f}-{c['maxh']:<5.0f} {hours:>9.1f} {hours*c['adv_th']:>8.0f}{ok}")
    choice = pick(cfg, cands, budget)
    if choice:
        c, hours, thh, cost = choice
        print(f"\nbest buy now: rig {c['id']} {c['name']} {c['adv_th']:.2f} TH for {hours:.1f} h "
              f"= {thh:.0f} TH-h, cost {fmt_btc(cost)} BTC")
    else:
        print("\nbudget too small for any rig's minimum rental length")


def cmd_status(cfg, api, args):
    cur = cfg["currency"]
    confirmed, unconfirmed = api.balance(cur)
    print(f"balance {cur}: confirmed {fmt_btc(confirmed)}  unconfirmed {fmt_btc(unconfirmed)}")
    rentals = api.active_rentals()
    if not rentals:
        print("no active rentals")
    for r in rentals:
        d = api.rental(r["id"])
        avg = d["hashrate"]["average"]
        print(f"rental {d['id']}: rig {d['rig']['id']} {d['rig']['name']} adv {d['hashrate']['advertised']['nice']} "
              f"avg {avg.get('nice')} ({avg.get('percent')}%)  {d['start']} -> {d['end']} "
              f"({rental_remaining_hours(d):.1f} h left)  paid {d['price']['paid']} {d['price']['currency']} "
              f"ended={d.get('ended')} refunded={d.get('refunded')}")
    st = load_state()
    if st["actions"]:
        print("last actions:")
        for a in st["actions"][-5:]:
            print(f"  {a['at']}  {a['summary']}")


def cmd_pools(cfg, api, args):
    p = api.profile(cfg["profile_id"])
    print(f"profile {p['id']} \"{p['name']}\" algo {p['algo']['name']}")
    for pool in sorted(p.get("pools") or [], key=lambda x: x["priority"]):
        host, port = pool["host"], int(pool["port"])
        try:
            addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
        except socket.gaierror:
            addrs = []
        reach = "no IPv4"
        if addrs:
            try:
                with socket.create_connection((addrs[0], port), timeout=6):
                    reach = "open"
            except OSError as e:
                reach = f"closed ({e.__class__.__name__})"
        print(f"  prio {pool['priority']}: {host}:{port} -> {addrs or '-'} [{reach}]  user {pool['user']}")


def cmd_run(cfg, api, args, live):
    state = api.state
    actions, ctx = decide(cfg, api)
    if not actions:
        print("nothing to do (no affordable rig, or all slots busy)")
        return
    print("\nplan:")
    for a in actions:
        print("  " + describe(a))
    print(f"budget left after plan: {fmt_btc(ctx['budget_left'])} BTC")
    if not live:
        print("\n(dry run; add --live to execute)")
        return
    for a in actions:
        summary = describe(a)
        try:
            if a["type"] == "rent":
                res = api.rent(a["rig"], a["hours"], cfg["profile_id"], cfg["currency"], a["price_type"], a["price_raw"])
                summary += f" -> rental {res.get('id')} paid {res.get('price', {}).get('paid')} ends {res.get('end')}"
            else:
                res = api.extend(a["rental"], a["hours"])
                summary += f" -> new_hrs {res.get('new_hrs')} cost {res.get('cost')}"
            ok = True
        except RuntimeError as e:
            summary += f" -> FAILED: {e}"
            ok = False
        print(summary)
        state["actions"].append({"at": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"), "ok": ok, "summary": summary,
                                 "action": a})
        save_state(state)
        telegram(cfg, ("✅ " if ok else "❌ ") + "MRR bot: " + summary)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("market"); m.add_argument("--top", type=int, default=15); m.add_argument("--budget", type=float)
    sub.add_parser("status"); sub.add_parser("plan"); sub.add_parser("pools")
    r = sub.add_parser("run"); r.add_argument("--live", action="store_true", help="actually spend")
    args = ap.parse_args()

    cfg = load_config()
    state = load_state()
    api = MRR(cfg, state)
    try:
        if args.cmd == "market":
            cmd_market(cfg, api, args)
        elif args.cmd == "status":
            cmd_status(cfg, api, args)
        elif args.cmd == "pools":
            cmd_pools(cfg, api, args)
        elif args.cmd == "plan":
            cmd_run(cfg, api, args, live=False)
        elif args.cmd == "run":
            if not cfg.get("profile_id"):
                sys.exit("profile_id missing in config.json")
            cmd_run(cfg, api, args, live=args.live)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
