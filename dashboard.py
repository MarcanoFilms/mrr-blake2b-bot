#!/usr/bin/env python3
"""MRR Command Center: local web dashboard for the rental bot and the DATUM gateway.

Serves www/index.html plus a JSON API on the LAN (HTTP Basic auth, password in
config.json "dashboard_password"). Reads the gateway's admin pages (digest auth),
the MRR API (through mrr_bot.py) and controls the systemd timer.
"""
import base64
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mrr_bot  # noqa: E402
import audit as audit_mod  # noqa: E402
import pyblock as pyb_mod  # noqa: E402

WWW = os.path.join(HERE, "www")
HISTORY_PATH = os.path.join(HERE, "history.json")
GATEWAY = {"url": "http://127.0.0.1:7158", "user": "oracle", "password": ""}
DASH_DEFAULTS = {
    "dashboard_port": 7160,
    "dashboard_password": "",
    "own_addresses": [],
    "gateway": GATEWAY,
    "datum_service": "datum-convoy.service",   # unit whose journal is shown in the dashboard
    "datum_service_scope": "user",             # "user" (systemctl --user) or "system"
    "read_only": False,                        # true: the dashboard never spends or touches the bot
    "bot_ui": True,                            # false: hide the bot's controls, history and log (rent by hand only)
    "rent_address": "",                        # payout address for rentals made here ("" = the profile's)
    "coin_label": "BTC",                       # how the dashboard names MRR's currency...
    "unit_label": "sats",                      # ...and its hundred-millionth
    # optional second unit for prices: {"label": "...", "url": "...", "path": "a.b", "invert": false}
    # the JSON value at `path` is how many of that coin one MRR coin buys (invert: the reverse)
    "alt_price": {},
}
EDITABLE_KNOBS = {
    "reserve_btc": float, "max_spend_per_run_btc": float, "max_concurrent_rentals": int,
    "max_hours": float, "min_hours": float, "min_rpi": float, "min_perf_ratio": float,
    "max_price_premium": float, "price_tolerance": float, "extend_window_hours": float,
    "fee_rate": float, "allow_new_rigs": bool, "excluded_regions": list, "excluded_rigs": list,
}
MRR_CACHE_TTL = 30
GATEWAY_POLL = 10
HISTORY_MAX = 24 * 3600 // GATEWAY_POLL

lock = threading.Lock()
SESSIONS = {}
AUDIT = [None]            # audit.Audit instance when enabled
AUDIT_PREFIX = ["mrr"]
PYB = [None]              # pyblock.PyBlock instance when enabled
PYB_PREFIX = ["pyb"]
LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MRR Command Center</title><link rel="icon" href="/favicon.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@500;600&family=JetBrains+Mono&display=swap">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;color:#dfe7f5;font:16px Rajdhani,system-ui,sans-serif;background:#05070d;
background-image:radial-gradient(900px 500px at 15% -10%,rgba(247,147,26,.18),transparent 60%),radial-gradient(800px 500px at 90% 10%,rgba(25,215,255,.15),transparent 60%),linear-gradient(rgba(25,215,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(25,215,255,.05) 1px,transparent 1px);background-size:auto,auto,42px 42px,42px 42px}
form{position:relative;background:rgba(12,17,30,.9);border:1px solid #2a3b5e;padding:30px 32px;width:min(380px,92vw);clip-path:polygon(16px 0,100% 0,100% calc(100% - 16px),calc(100% - 16px) 100%,0 100%,0 16px);box-shadow:0 0 40px rgba(25,215,255,.12)}
form::before{content:"";position:absolute;left:0;top:0;width:100%;height:1px;background:linear-gradient(90deg,#f7931a,transparent 60%)}
h1{font:700 16px Orbitron,sans-serif;letter-spacing:2.5px;text-transform:uppercase;margin:0 0 6px}h1 span{color:#f7931a;text-shadow:0 0 14px rgba(247,147,26,.6)}
p{color:#7f8db0;margin:0 0 18px;font-size:12px;letter-spacing:2px;text-transform:uppercase}
input{width:100%;box-sizing:border-box;padding:11px;border:1px solid #2a3b5e;background:#080c15;color:#dfe7f5;font:15px "JetBrains Mono",monospace;margin-bottom:14px;outline:none}input:focus{border-color:#19d7ff;box-shadow:0 0 10px rgba(25,215,255,.3)}
button{width:100%;padding:11px;border:0;background:linear-gradient(135deg,#f7931a,#d9780e);color:#140a00;font:700 13px Rajdhani,sans-serif;letter-spacing:2px;text-transform:uppercase;cursor:pointer;clip-path:polygon(8px 0,100% 0,calc(100% - 8px) 100%,0 100%)}button:hover{box-shadow:0 0 18px rgba(247,147,26,.55)}
.err{color:#ff3b5c;font-size:13px;margin-bottom:10px;min-height:1em}
.cover{display:block;width:72%;margin:0 auto 18px;border-radius:50%;box-shadow:0 0 40px rgba(25,215,255,.35),0 0 60px rgba(247,147,26,.25)}</style></head>
<body><form method="post" action="/login"><img class="cover" src="/img/cover.jpg" alt=""><h1>MRR <span>Command Center</span></h1><p>Blake2B &middot; DATUM gateway &middot; alquileres</p>
<div class="err">{{msg}}</div><input type="password" name="password" placeholder="contrase&ntilde;a del dashboard" autofocus autocomplete="current-password"><button type="submit">Entrar</button></form></body></html>"""
STATIC = {"/img/cover.jpg": ("img/cover.jpg", "image/jpeg"), "/img/logo-96.png": ("img/logo-96.png", "image/png"),
          "/img/logo-192.png": ("img/logo-192.png", "image/png"), "/favicon.png": ("img/favicon.png", "image/png")}
cache = {"mrr": {"at": 0, "data": None, "error": None}, "gateway": {"at": 0, "data": None, "error": None}}
history = []           # [{"t": epoch, "th": total_th, "clients": n}]
rdns_cache = {}


# ----------------------------------------------------------------------------- config
def cfg():
    c = mrr_bot.load_config()
    for k, v in DASH_DEFAULTS.items():
        c.setdefault(k, v)
    return c


def ensure_dashboard_config():
    with open(mrr_bot.CONFIG_PATH) as f:
        raw = json.load(f)
    changed = False
    if not raw.get("dashboard_password"):
        raw["dashboard_password"] = secrets.token_urlsafe(12)
        changed = True
    raw.setdefault("dashboard_port", 7160)
    raw.setdefault("own_addresses", [])
    raw.setdefault("gateway", dict(GATEWAY))
    raw.setdefault("datum_service", DASH_DEFAULTS["datum_service"])
    if changed or "dashboard_port" not in raw:
        with open(mrr_bot.CONFIG_PATH, "w") as f:
            json.dump(raw, f, indent=1)
    return raw


def write_knobs(updates):
    with open(mrr_bot.CONFIG_PATH) as f:
        raw = json.load(f)
    applied = {}
    for k, v in updates.items():
        if k not in EDITABLE_KNOBS:
            continue
        typ = EDITABLE_KNOBS[k]
        try:
            if typ is bool:
                v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
            elif typ is list:
                v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
            else:
                v = typ(v)
        except (TypeError, ValueError):
            continue
        if typ is float and v < 0:
            continue
        raw[k] = v
        applied[k] = v
    with open(mrr_bot.CONFIG_PATH, "w") as f:
        json.dump(raw, f, indent=1)
    return applied


# ----------------------------------------------------------------------------- gateway
def gateway_opener(c):
    g = c["gateway"]
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, g["url"], g["user"], g["password"])
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))


def gateway_get(c, path):
    with gateway_opener(c).open(c["gateway"]["url"] + path, timeout=8) as r:
        return r.read().decode(errors="replace")


def gateway_post_cmd(c, payload):
    page = gateway_get(c, "/clients")
    m = re.search(r"data\.csrf = '([0-9a-f]+)'", page)
    if not m:
        raise RuntimeError("csrf token not found on gateway page")
    payload = dict(payload, csrf=m.group(1))
    req = urllib.request.Request(c["gateway"]["url"] + "/cmd", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with gateway_opener(c).open(req, timeout=8) as r:
        return r.status


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


HASH_UNITS = {"h": 1e-12, "kh": 1e-9, "mh": 1e-6, "gh": 1e-3, "th": 1.0, "ph": 1e3, "eh": 1e6}


def parse_hashrate_th(s):
    m = re.match(r"([\d.]+)\s*([KMGTPE]?)h/s", s.strip(), re.I)
    if not m:
        return 0.0
    return float(m.group(1)) * HASH_UNITS.get((m.group(2) + "h").lower(), 1.0)


def rdns(ip):
    if ip in rdns_cache:
        return rdns_cache[ip]
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        name = ""
    rdns_cache[ip] = name
    return name


def classify_host(name, ip):
    if "miningrigrentals" in name:
        return "MRR proxy"
    if "pyblock" in name or "anteldata" in name:
        return "PyBLOCK rental"
    if ip.startswith(("192.168.", "10.", "127.")):
        return "LAN"
    return ""


def parse_clients(html, own_addresses):
    rows = re.findall(r"<TR>(.*?)</TR>", html, re.S)
    clients = []
    for row in rows:
        cells = re.findall(r"<TD>(.*?)</TD>", row, re.S)
        if len(cells) < 2 or "TID/CID" in cells[0]:
            continue
        tid_cid, rem = strip_tags(cells[0]), strip_tags(cells[1]).replace("::ffff:", "")
        kick = re.search(r"tid:(\d+),cid:(\d+),t:(\d+),id:(\d+)", row)
        entry = {"tid_cid": tid_cid, "ip": rem, "host": rdns(rem), "subscribed": False,
                 "kick": {"tid": int(kick.group(1)), "cid": int(kick.group(2))} if kick else None,
                 # unique per connection (thread, client slot, connect time, id): tells reconnections apart
                 "skey": "_".join(kick.groups()) if kick else None}
        entry["label"] = classify_host(entry["host"], rem)
        if len(cells) < 9 or "Not Subscribed" in cells[2]:
            clients.append(entry)
            continue
        user = strip_tags(cells[2])
        addr, _, worker = user.partition(".")
        subbed = strip_tags(cells[3])
        sid = re.search(r"([0-9a-f]{8})", subbed)
        acc = re.match(r"([\d.]+[KMGTPE]?)\s*\((\d+)\)", strip_tags(cells[6]))
        rej = re.match(r"([\d.]+[KMGTPE]?)\s*\((\d+)\)\s*([\d.]+)%", strip_tags(cells[7]))
        hr = strip_tags(cells[8])
        hr_m = re.match(r"(.*?h/s)\s*\(([\d.]+)s\)", hr, re.I)
        if not entry["label"] and re.fullmatch(re.escape(AUDIT_PREFIX[0]) + r"\d+", worker or ""):
            entry["label"] = "MRR proxy"   # worker given by the audit; the proxies' rDNS is not always set
        if not entry["label"] and re.fullmatch(re.escape(PYB_PREFIX[0]) + r"[0-9a-f]{6}", worker or ""):
            entry["label"] = "PyBLOCK rental"   # worker given to PyBLØCK orders made here
        entry.update({
            "subscribed": True, "user": user, "address": addr, "worker": worker,
            "own": (addr in own_addresses) if own_addresses else None,
            "session": sid.group(1) if sid else "", "subbed_s": float(re.search(r"([\d.]+)s", subbed).group(1)) if re.search(r"([\d.]+)s", subbed) else None,
            "last_accepted": strip_tags(cells[4]), "vdiff": strip_tags(cells[5]),
            "diff_accepted": acc.group(1) if acc else "", "shares_accepted": int(acc.group(2)) if acc else 0,
            "diff_rejected": rej.group(1) if rej else "", "shares_rejected": int(rej.group(2)) if rej else 0,
            "reject_pct": float(rej.group(3)) if rej else 0.0,
            "hashrate": hr_m.group(1) if hr_m else hr, "hashrate_th": parse_hashrate_th(hr_m.group(1) if hr_m else hr),
            "hashrate_age_s": float(hr_m.group(2)) if hr_m else None,
            "coinbase": strip_tags(cells[9]) if len(cells) > 9 else "",
            "useragent": strip_tags(cells[10]) if len(cells) > 10 else "",
        })
        clients.append(entry)
    return clients


def parse_status(html):
    text = re.sub(r"<[^>]+>", "\n", html)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = {}
    for i, l in enumerate(lines):
        if l.endswith(":") and i + 1 < len(lines) and not lines[i + 1].endswith(":"):
            out[l[:-1]] = lines[i + 1]
    wanted = {
        "Status": "status", "Pool Host": "pool_host", "Pool Tag": "pool_tag", "Pool Current MinDiff": "pool_mindiff",
        "Pool Shares Accepted": "pool_shares_accepted", "Pool Shares Rejected": "pool_shares_rejected",
        "Local Shares Accepted": "local_shares_accepted", "Local Shares Rejected": "local_shares_rejected",
        "Process uptime": "uptime", "Total Connections": "connections", "Total Work Subscriptions": "subscriptions",
        "Estimated Hashrate": "hashrate", "Block Height": "block_height", "Block Value": "block_value",
        "Job ID": "job_id", "Txn Count": "txn_count",
    }
    return {v: out.get(k, "") for k, v in wanted.items()}


def poll_gateway():
    c = cfg()
    try:
        clients = parse_clients(gateway_get(c, "/clients"), c["own_addresses"])
        status = parse_status(gateway_get(c, "/"))
        total = sum(x.get("hashrate_th", 0) for x in clients)
        subs = [x for x in clients if x["subscribed"]]
        data = {"ok": True, "status": status, "clients": clients, "total_th": total,
                "subscribed": len(subs), "foreign": sum(1 for x in subs if x.get("own") is False),
                "at": time.time()}
        err = None
    except Exception as e:
        data, err = None, str(e)
    if data and AUDIT[0]:
        try:
            AUDIT[0].record(clients, GATEWAY_POLL)
        except Exception as e:
            print(f"[audit record failed: {e}]", file=sys.stderr, flush=True)
    with lock:
        cache["gateway"] = {"at": time.time(), "data": data, "error": err}
        if data:
            history.append({"t": int(data["at"]), "th": round(total, 3), "clients": len(subs)})
            del history[:-HISTORY_MAX]


def gateway_loop():
    n = 0
    while True:
        poll_gateway()
        n += 1
        if n % 30 == 0:
            save_history()
        time.sleep(GATEWAY_POLL)


def save_history():
    with lock:
        snap = list(history)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, HISTORY_PATH)


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                history.extend(json.load(f)[-HISTORY_MAX:])
        except (ValueError, OSError):
            pass


# ----------------------------------------------------------------------------- MRR
def mrr_api(c):
    return mrr_bot.MRR(c, mrr_bot.load_state())


def mrr_overview(force=False):
    with lock:
        ent = cache["mrr"]
        if not force and ent["data"] and time.time() - ent["at"] < MRR_CACHE_TTL:
            return ent["data"], ent["error"]
    c = cfg()
    try:
        api = mrr_api(c)
        confirmed, unconfirmed = api.balance(c["currency"])
        stats = api.algo_stats(c["algo"])["stats"]
        rentals = []
        for r in api.active_rentals():
            try:
                d = api.rental(r["id"])
            except RuntimeError:
                d = r
            left = mrr_bot.rental_remaining_hours(d)
            length = float(d.get("length") or 0)
            avg = (d.get("hashrate") or {}).get("average") or {}
            rentals.append({
                "id": d["id"], "rig_id": d["rig"]["id"], "rig_name": d["rig"]["name"], "region": d["rig"].get("region"),
                "rpi": d["rig"].get("rpi"), "advertised": d["hashrate"]["advertised"]["nice"],
                "advertised_th": mrr_bot.to_th(d["hashrate"]["advertised"]),
                "average": avg.get("nice"), "average_pct": avg.get("percent"),
                "average_th": mrr_bot.to_th(avg) if avg else None,
                "start_unix": int(mrr_bot.parse_mrr_time(d["start"]).timestamp()),
                "end_unix": int(mrr_bot.parse_mrr_time(d["end"]).timestamp()),
                "start": d.get("start"), "end": d.get("end"), "length_h": length, "left_h": max(0.0, left),
                "progress": max(0.0, min(1.0, 1 - left / length)) if length else 0,
                "paid": d["price"].get("paid"), "currency": d["price"].get("currency"),
                "maxhours": d["rig"].get("maxhours"), "ended": d.get("ended"),
                "extensions": bool(d["rig"].get("extensions")),
                "paid_sat_th_day": paid_sat_th_day(d["price"].get("paid"), mrr_bot.to_th(d["hashrate"]["advertised"]),
                                                   length, c.get("fee_rate")),
            })
        try:
            prices = rig_prices([r["rig_id"] for r in rentals], c)
        except Exception as e:    # the renewal price is a nicety: never lose the overview over it
            prices = {}
            print(f"[rig prices failed: {e}]", file=sys.stderr, flush=True)
        for r in rentals:
            r["renew_sat_th_day"] = prices.get(str(r["rig_id"]))
        data = {"balance": {"confirmed": confirmed, "unconfirmed": unconfirmed, "currency": c["currency"]},
                "market": {"last_10": float(stats["prices"]["last_10"]["amount"]),
                           "lowest": float(stats["prices"]["lowest"]["amount"]),
                           "last": float(stats["prices"]["last"]["amount"]),
                           "available_rigs": int(stats["available"]["rigs"]),
                           "available_hash": stats["available"]["hash"]["nice"],
                           "rented_rigs": int(stats["rented"]["rigs"]), "rented_hash": stats["rented"]["hash"]["nice"]},
                "rentals": rentals, "at": time.time()}
        err = None
        api.state and mrr_bot.save_state(api.state)
    except Exception as e:
        data, err = None, str(e)
    with lock:
        if data or not cache["mrr"]["data"]:
            cache["mrr"] = {"at": time.time(), "data": data, "error": err}
        else:
            cache["mrr"]["error"] = err
        return cache["mrr"]["data"], cache["mrr"]["error"]


def paid_sat_th_day(paid_btc, adv_th, length_h, fee_rate):
    """What a rental cost in sats/TH/day: MRR's `paid` (BTC, without its fee, which it
    charges apart as "Rental Fee") plus that fee, over the advertised TH/s and its length."""
    try:
        paid, th, hours = float(paid_btc), float(adv_th), float(length_h)
    except (TypeError, ValueError):
        return None
    if paid <= 0 or th <= 0 or hours <= 0:
        return None
    return paid * 1e8 * (1 + float(fee_rate or 0)) / th / (hours / 24.0)


def rig_price_sat_th_day(rig, currency, fee_rate):
    """A rig's current price in sats/TH/day with MRR's fee: what extending it costs now
    (the same as /rental/<id>/extend with getcost, which also adds the fee)."""
    p = ((rig or {}).get("price") or {}).get(currency) or {}
    try:
        price = float(p.get("price") or 0)
    except (TypeError, ValueError):
        return None
    unit = (rig["price"].get("type") or "th").lower()
    if price <= 0 or unit not in mrr_bot.UNIT:
        return None
    return price / mrr_bot.UNIT[unit] * 1e8 * (1 + float(fee_rate or 0))


RIG_PRICE_TTL = 600
_rig_prices = {"at": 0, "ids": frozenset(), "data": {}}


def rig_prices(rig_ids, c):
    """{rig_id: sats/TH/day to extend now}, one batched MRR call (GET /rig/a;b;c), cached 10 min."""
    ids = frozenset(str(i) for i in rig_ids if i)
    if not ids:
        return {}
    if ids <= _rig_prices["ids"] and time.time() - _rig_prices["at"] < RIG_PRICE_TTL:
        return _rig_prices["data"]
    api = mrr_api(c)
    try:
        res = api.call("GET", "/rig/" + ";".join(sorted(ids)))
    finally:
        mrr_bot.save_state(api.state)
    rigs = res if isinstance(res, list) else [res]
    data = {str(r.get("id")): rig_price_sat_th_day(r, c["currency"], c.get("fee_rate")) for r in rigs}
    _rig_prices.update(at=time.time(), ids=ids, data=data)
    return data


_alt = {"at": 0, "rate": None, "error": None}
_graphs = {}


def alt_rate():
    """How many coins of the optional second unit one MRR coin buys, cached 10 min (None if unset)."""
    a = cfg().get("alt_price") or {}
    if not a.get("url"):
        return None, None
    if time.time() - _alt["at"] < 600:
        return _alt["rate"], _alt["error"]
    try:
        req = urllib.request.Request(a["url"], headers={"User-Agent": "mrr-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            v = json.load(r)
        for k in (a.get("path") or "").split("."):
            if k:
                v = v[k]
        v = float(v)
        rate = (1 / v) if a.get("invert") else v
        _alt.update(at=time.time(), rate=rate if rate > 0 else None, error=None)
    except Exception as e:
        _alt.update(at=time.time(), error=str(e)[:120])
    return _alt["rate"], _alt["error"]


def rig_graph(rig_id, hours=24, points=48):
    """A rig's recent hashrate from MRR (TH/s), averaged into `points` buckets, cached 10 min."""
    key = (int(rig_id), int(hours))
    hit = _graphs.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    api = mrr_api(cfg())
    g = api.call("GET", f"/rig/{key[0]}/graph", {"hours": key[1], "deflation": "true"})
    mrr_bot.save_state(api.state)
    cd = g.get("chartdata") or {}
    bars = json.loads("[" + (cd.get("bars") or "") + "]")
    t0, t1 = int(cd.get("timestamp_start") or 0), int(cd.get("timestamp_end") or 0)
    out = []
    if bars and t1 > t0:
        step = (t1 - t0) / points
        sums = [[0.0, 0] for _ in range(points)]
        for ms, h in bars:
            i = min(points - 1, max(0, int((ms / 1000 - t0) / step)))
            sums[i][0] += float(h)
            sums[i][1] += 1
        out = [round(s / n / 1e12, 3) if n else None for s, n in sums]
    res = {"rig": key[0], "hours": key[1], "t0": t0, "t1": t1, "th": out,
           "advertised_th": None, "at": time.time()}
    _graphs[key] = (time.time(), res)
    if len(_graphs) > 300:
        for k in sorted(_graphs, key=lambda k: _graphs[k][0])[:100]:
            _graphs.pop(k, None)
    return res


def market_view(budget=None):
    c = cfg()
    api = mrr_api(c)
    confirmed, _ = api.balance(c["currency"])
    market = float(api.algo_stats(c["algo"])["stats"]["prices"]["last_10"]["amount"])
    cands = mrr_bot.candidate_rigs(c, api.rigs(c["algo"]), market)
    budget = budget if budget is not None else max(0.0, confirmed - c["reserve_btc"])
    fee = 1 + c["fee_rate"]
    rows = []
    for x in cands[:40]:
        hours = min(x["maxh"], budget / (x["hour_cost"] * fee)) if budget > 0 else 0
        rows.append(dict(x, afford_h=round(hours, 1), afford_thh=round(hours * x["adv_th"]),
                         sat_th_day=round(x["price_th_day"] * 1e8),
                         affordable=hours >= x["minh"], hour_cost_total=x["hour_cost"] * fee))
    choice = mrr_bot.pick(c, cands, budget)
    best = None
    if choice:
        b, hours, thh, cost = choice
        best = {"rig": b["id"], "name": b["name"], "adv_th": b["adv_th"], "hours": hours, "th_hours": thh, "cost": cost}
        # The table lists the 40 cheapest; the best buy can be further down (a big
        # rig at a higher price), so make sure it is always in the table.
        if not any(r["id"] == b["id"] for r in rows):
            h = min(b["maxh"], budget / (b["hour_cost"] * fee)) if budget > 0 else 0
            rows.append(dict(b, afford_h=round(h, 1), afford_thh=round(h * b["adv_th"]),
                             sat_th_day=round(b["price_th_day"] * 1e8),
                             affordable=h >= b["minh"], hour_cost_total=b["hour_cost"] * fee))
    mrr_bot.save_state(api.state)
    return {"budget": budget, "market": market, "rigs": rows, "best": best, "at": time.time()}


def plan_view():
    c = cfg()
    api = mrr_api(c)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        actions, ctx = mrr_bot.decide(c, api)
    mrr_bot.save_state(api.state)
    return {"actions": actions, "descriptions": [mrr_bot.describe(a) for a in actions],
            "budget_left": ctx["budget_left"], "log": buf.getvalue()}


ADDRESS_RE = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")


def set_rent_address(address):
    address = (address or "").strip()
    if address and not ADDRESS_RE.match(address):
        raise RuntimeError("that does not look like a mainnet address (bc1… / 1… / 3…)")
    with open(mrr_bot.CONFIG_PATH) as f:
        raw = json.load(f)
    raw["rent_address"] = address
    tmp = mrr_bot.CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, mrr_bot.CONFIG_PATH)
    return address


def manual_rent(rig_id, hours):
    """Rent one rig for the given hours, only when the user presses Rent and confirms.

    The rental mines to the pool profile, then its pool user is rewritten to
    <rent_address>.<worker_prefix><rental id>: a worker of its own (so the audit can
    tell it apart from the start) paying to the address fixed on the dashboard."""
    c = cfg()
    api = mrr_api(c)
    rig = api.call("GET", f"/rig/{int(rig_id)}")
    rig = rig[0] if isinstance(rig, list) else rig
    p = rig["price"][c["currency"]]
    hours = float(hours)
    minh = max(float(rig.get("minhours") or 0), float(p.get("min_rental_length") or 0))
    maxh = float(rig.get("maxhours") or c["max_hours"])
    if not (minh <= hours <= maxh):
        raise RuntimeError(f"hours must be between {minh} and {maxh} for this rig")
    cost = float(p["hour"]) * hours * (1 + c["fee_rate"])
    confirmed, _ = api.balance(c["currency"])
    if cost > confirmed:
        raise RuntimeError(f"insufficient confirmed balance: need {cost:.8f}, have {confirmed:.8f}")
    res = api.rent(rig["id"], hours, c["profile_id"], c["currency"], rig["price"]["type"], float(p["price"]))
    rid = res.get("id")
    worker = address = None
    note = ""
    if rid:
        prefix = audit_mod.settings(c)["worker_prefix"]
        worker = f"{prefix}{rid}"
        try:
            pool = audit_mod.top_pool(api, rid)
            address = (c.get("rent_address") or "").strip() or (pool or {}).get("user", "").split(".", 1)[0]
            if pool and address:
                audit_mod.set_pool_user(api, rid, pool, address, worker)
                if AUDIT[0]:
                    AUDIT[0].remember(rid, worker, None, address)
        except Exception as e:   # the rental exists and mines to the profile either way
            note = f" (pool user not changed: {e})"
            worker = None
    summary = (f"MANUAL RENT rig {rig['id']} \"{rig['name']}\" {rig['hashrate']['advertised']['nice']} for {hours:.2f} h"
               f" -> rental {rid} paid {res.get('price', {}).get('paid')} ends {res.get('end')}"
               + (f", worker {worker} paying to {address}" if worker else "") + note)
    st = api.state
    st["actions"].append({"at": mrr_bot.now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"), "ok": True, "summary": summary,
                          "action": {"type": "manual_rent", "rig": rig["id"], "hours": hours}})
    mrr_bot.save_state(st)
    mrr_bot.telegram(c, "✅ MRR dashboard: " + summary)
    mrr_overview(force=True)
    return {"summary": summary, "rental": res, "worker": worker, "address": address}


def extend_rental(rental_id, hours, quote_only):
    c = cfg()
    api = mrr_api(c)
    if quote_only:
        # Estimated from the rig's current price (cached), NOT with MRR's getcost: MRR counts a
        # getcost as an extension request and then refuses the real one right after with "Extension
        # request too soon after the previous" (15 nov 2026).
        data, _ = mrr_overview()
        r = next((x for x in ((data or {}).get("rentals") or []) if str(x.get("id")) == str(rental_id)), None)
        if not r:
            raise ValueError(f"rental {rental_id} not found")
        h = float(hours)
        price = r.get("renew_sat_th_day")
        room = (float(r["maxhours"]) - float(r["length_h"])) if r.get("maxhours") else None
        return {"quote": {
            "estimated": True,
            "cost_sats": round(price * float(r["advertised_th"] or 0) * h / 24) if price else None,
            "sat_th_day": round(price) if price else None,
            "new_hrs": round(float(r["length_h"]) + h, 2),
            "room_h": room, "extensions": r.get("extensions")}}
    res = api.extend(rental_id, float(hours))
    if isinstance(res, list):        # MRR answers a list, one entry per rental
        res = res[0] if res else {}
    if isinstance(res, dict) and res.get("success") is False:
        raise RuntimeError("MRR: " + str(res.get("message") or "extension refused"))
    summary = f"MANUAL EXTEND rental {rental_id} by {float(hours):.2f} h -> cost {res.get('cost')} new_hrs {res.get('new_hrs')}"
    st = api.state
    st["actions"].append({"at": mrr_bot.now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"), "ok": True, "summary": summary,
                          "action": {"type": "manual_extend", "rental": rental_id, "hours": float(hours)}})
    mrr_bot.save_state(st)
    mrr_bot.telegram(c, "✅ MRR dashboard: " + summary)
    mrr_overview(force=True)
    return {"summary": summary, "result": res}


# ----------------------------------------------------------------------------- PyBLØCK
def mrr_ref_sats():
    """MRR's market price (last 10 rentals) in sats/TH/day with MRR's fee: what PyBLØCK is compared with."""
    with lock:
        m = (cache["mrr"]["data"] or {}).get("market") or {}
    if not m.get("last_10"):
        return None
    return float(m["last_10"]) * 1e8 * (1 + float(cfg().get("fee_rate") or 0))


def profile_address():
    """Payout address of the MRR pool profile (the user of its first pool, before the worker)."""
    c = cfg()
    api = mrr_api(c)
    p = api.profile(c["profile_id"])
    mrr_bot.save_state(api.state)
    pools = sorted(p.get("pools") or [], key=lambda x: int(x.get("priority") or 0))
    return (pools[0].get("user") or "").split(".", 1)[0] if pools else ""


def pyb_analyse(worker, start, end, th):
    """The audit's measure (shares x vdiff since the start) for a PyBLØCK order's worker."""
    if not AUDIT[0] or not worker or not start or not th:
        return None
    return AUDIT[0].analyse({"start_unix": start, "end_unix": end, "advertised_th": th}, worker, None, False)


def gateway_clients():
    with lock:
        return list((cache["gateway"]["data"] or {}).get("clients") or [])


# ----------------------------------------------------------------------------- systemd
def sysctl(*args, timeout=20):
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def bot_state():
    _, enabled = sysctl("is-enabled", "mrr-bot.timer")
    _, active = sysctl("is-active", "mrr-bot.timer")
    _, svc = sysctl("is-active", "mrr-bot.service")
    _, props = sysctl("show", "mrr-bot.timer", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec")
    nxt = last = ""
    for line in props.splitlines():
        k, _, v = line.partition("=")
        if k == "NextElapseUSecRealtime":
            nxt = v
        elif k == "LastTriggerUSec":
            last = v
    return {"timer_enabled": enabled == "enabled", "timer_active": active == "active",
            "service_running": svc in ("active", "activating"), "next_run": nxt, "last_run": last}


def bot_log(lines=80):
    r = subprocess.run(["journalctl", "--user", "-u", "mrr-bot.service", "-n", str(lines), "--no-pager", "-o", "short-iso"],
                       capture_output=True, text=True, timeout=20)
    return r.stdout.strip().splitlines()


def gateway_log(lines=40):
    unit = cfg().get("datum_service") or DASH_DEFAULTS["datum_service"]
    scope = ["--user"] if (cfg().get("datum_service_scope") or "user") == "user" else []
    r = subprocess.run(["journalctl", *scope, "-u", unit, "-n", "400", "--no-pager", "-o", "short-iso"],
                       capture_output=True, text=True, timeout=20)
    keep = [l for l in r.stdout.splitlines()
            if re.search(r"INFO|WARN|ERROR|FATAL|New client \(|Kicking client|closed\.", l) and "datum_api_answer" not in l]
    return keep[-lines:]


# ----------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "mrr-dashboard/1.0"

    def log_message(self, fmt, *args):
        if "/api/" not in (args[0] if args else ""):
            return

    def _has_session(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "mrr_session" and v in SESSIONS:
                return True
        return False

    def _auth_ok(self):
        pw = cfg().get("dashboard_password") or ""
        if not pw or self._has_session():
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, got = base64.b64decode(hdr[6:]).decode().partition(":")
                if secrets.compare_digest(got, pw):
                    return True
            except Exception:
                pass
        if self.path.startswith("/api/"):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"auth required"}')
        else:
            self._login_page()
        return False

    def _login_page(self, wrong=False):
        body = LOGIN_HTML.replace("{{msg}}", "Contraseña incorrecta" if wrong else "").encode()
        self.send_response(401 if not wrong else 403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _login(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        got = (form.get("password") or [""])[0]
        pw = cfg().get("dashboard_password") or ""
        if not secrets.compare_digest(got, pw):
            time.sleep(1)
            return self._login_page(wrong=True)
        tok = secrets.token_urlsafe(24)
        SESSIONS[tok] = time.time()
        self.send_response(303)
        self.send_header("Set-Cookie", f"mrr_session={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
        self.send_header("Location", "/")
        self.end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        path = os.path.join(WWW, name)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in STATIC:
            return self._file(*STATIC[url.path])
        if not self._auth_ok():
            return
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if url.path == "/api/overview":
                c = cfg()
                mrr, mrr_err = mrr_overview(force="force" in q)
                with lock:
                    gw = dict(cache["gateway"])
                    hist = list(history)
                st = mrr_bot.load_state()
                return self._json({
                    "now": time.time(), "mrr": mrr, "mrr_error": mrr_err,
                    "gateway": gw["data"], "gateway_error": gw["error"],
                    "history": hist[-(6 * 3600 // GATEWAY_POLL):] if "full" not in q else hist,
                    "bot": dict(bot_state(), actions=st.get("actions", [])[-15:]),
                    "config": {k: c.get(k) for k in EDITABLE_KNOBS},
                    "profile_id": c.get("profile_id"), "own_addresses": c.get("own_addresses"),
                    "datum_service": c.get("datum_service"),
                    "read_only": bool(c.get("read_only")), "bot_ui": bool(c.get("bot_ui")),
                    "rent_address": c.get("rent_address") or "",
                    "labels": {"coin": c.get("coin_label") or "BTC", "unit": c.get("unit_label") or "sats",
                               "alt": (c.get("alt_price") or {}).get("label")},
                    "alt_rate": alt_rate()[0],
                    "pyblock": PYB[0].view() if PYB[0] else {"enabled": False},
                })
            if url.path == "/api/audit":
                if not AUDIT[0]:
                    return self._json({"enabled": False})
                mrr, mrr_err = mrr_overview()
                return self._json(dict(AUDIT[0].view((mrr or {}).get("rentals") or []), enabled=True, mrr_error=mrr_err))
            if url.path == "/api/market":
                b = q.get("budget", [None])[0]
                return self._json(market_view(float(b) if b else None))
            if url.path == "/api/history":
                return self._json(AUDIT[0].history() if AUDIT[0] else {"history": [], "favorites": []})
            if url.path == "/api/rig-graph":
                return self._json(rig_graph(int(q["id"][0]), int(q.get("hours", ["24"])[0])))
            if url.path == "/api/pyblock":
                return self._json(PYB[0].view(force="force" in q) if PYB[0] else {"enabled": False})
            if url.path == "/api/pyblock/gateway-test" and PYB[0]:
                return self._json(PYB[0].gateway_test(force="force" in q))
            if url.path == "/api/pyblock/open" and PYB[0]:
                link = PYB[0].link(q.get("ref", [""])[0])
                if not link:
                    return self._json({"error": "sin enlace para ese pedido"}, 404)
                self.send_response(302)
                self.send_header("Location", link)
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if url.path == "/api/plan" and cfg().get("bot_ui"):
                return self._json(plan_view())
            if url.path == "/api/log":
                return self._json({"bot": bot_log() if cfg().get("bot_ui") else [], "gateway": gateway_log()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        if self.path == "/login":
            return self._login()
        if not self._auth_ok():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        try:
            c = cfg()
            if c.get("read_only") and (self.path in ("/api/bot", "/api/config", "/api/rent", "/api/rent-address",
                                                     "/api/pyblock/order") or (
                    self.path == "/api/extend" and not body.get("quote_only"))):
                return self._json({"ok": False, "error": "read-only dashboard (read_only in config.json)"}, 403)
            if not c.get("bot_ui") and self.path in ("/api/bot", "/api/config"):
                return self._json({"ok": False, "error": "the bot is switched off here (bot_ui in config.json)"}, 403)
            if self.path == "/api/favorite":
                if not AUDIT[0]:
                    return self._json({"error": "audit is off"}, 400)
                AUDIT[0].favorite(int(body["rig"]), body.get("name"), bool(body.get("on", True)))
                return self._json({"ok": True})
            if self.path == "/api/rent-address":
                return self._json({"ok": True, "rent_address": set_rent_address(body.get("address"))})
            if self.path == "/api/bot":
                action = body.get("action")
                if action == "enable":
                    rc, out = sysctl("enable", "--now", "mrr-bot.timer")
                elif action == "disable":
                    rc, out = sysctl("disable", "--now", "mrr-bot.timer")
                elif action == "run":
                    rc, out = sysctl("start", "--no-block", "mrr-bot.service")
                else:
                    return self._json({"error": "unknown action"}, 400)
                return self._json({"ok": rc == 0, "output": out, "bot": bot_state()})
            if self.path == "/api/config":
                return self._json({"ok": True, "applied": write_knobs(body)})
            if self.path == "/api/rent":
                return self._json(dict(ok=True, **manual_rent(body["rig"], body["hours"])))
            if self.path == "/api/extend":
                return self._json(dict(ok=True, **extend_rental(body["rental"], body["hours"], body.get("quote_only", False))))
            if self.path.startswith("/api/pyblock/"):
                if not PYB[0]:
                    return self._json({"ok": False, "error": "PyBLØCK está desactivado (pyblock.enabled)"}, 400)
                try:
                    if self.path == "/api/pyblock/order":
                        o = PYB[0].create(str(body.get("package") or ""), int(body.get("qty") or 1),
                                          str(body.get("pay") or "ln"), str(body.get("client_nonce") or ""))
                        return self._json({"ok": True, "order": o})
                    if self.path == "/api/pyblock/refresh":
                        rec = PYB[0]._find(ref=str(body.get("ref") or ""))
                        if not rec:
                            return self._json({"ok": False, "error": "no existe ese pedido"}, 404)
                        return self._json({"ok": True, "order": PYB[0].public(PYB[0].refresh(rec, force=True))})
                    if self.path == "/api/pyblock/add":
                        o = PYB[0].add_external(str(body.get("order") or ""), str(body.get("worker") or ""))
                        return self._json({"ok": True, "order": o})
                    if self.path == "/api/pyblock/forget":
                        PYB[0].forget(str(body.get("ref") or ""))
                        return self._json({"ok": True})
                except pyb_mod.OrderError as e:
                    return self._json({"ok": False, "error": str(e), "retryable": e.retryable,
                                       "status": e.status}, 409 if not e.retryable else 503)
                return self._json({"error": "not found"}, 404)
            if self.path == "/api/gateway/kick":
                status = gateway_post_cmd(cfg(), {"cmd": "kill_client", "tid": int(body["tid"]), "cid": int(body["cid"])})
                threading.Thread(target=poll_gateway, daemon=True).start()
                return self._json({"ok": status < 400, "status": status})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)


def main():
    raw = ensure_dashboard_config()
    load_history()
    c = cfg()
    a = audit_mod.settings(c)
    if a["enabled"]:
        AUDIT[0] = audit_mod.Audit(HERE, c)
        AUDIT_PREFIX[0] = a["worker_prefix"]
        if c.get("api_key") and c.get("api_secret"):
            threading.Thread(target=audit_mod.sync_loop, daemon=True,
                             args=(AUDIT[0], lambda: mrr_api(cfg()), lambda api: mrr_bot.save_state(api.state))).start()
    p = pyb_mod.settings(c)
    if p.get("enabled", True):
        PYB_PREFIX[0] = p.get("worker_prefix") or "pyb"
        PYB[0] = pyb_mod.PyBlock(HERE, cfg, notify=mrr_bot.telegram, gateway_clients=gateway_clients,
                                 mrr_ref=mrr_ref_sats, address_fallback=profile_address, analyse=pyb_analyse)
        threading.Thread(target=PYB[0].loop, daemon=True).start()
    threading.Thread(target=gateway_loop, daemon=True).start()
    port = int(raw.get("dashboard_port") or 7160)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"MRR Command Center on http://0.0.0.0:{port}/ (user: any, password in config.json)", flush=True)
    try:
        srv.serve_forever()
    finally:
        save_history()


if __name__ == "__main__":
    main()
