"""P2Hash (PyBLØCK's hashrate market on Nostr): rent BLAKE2b rigs per tranche.

The protocol is the one of the PyBLØCK Hashrate Agent ("Rent" tab, MIT,
gitlab.com/astrolexis-group1/pyblock-hashrate-agent, miner/rent.mjs):

  * sellers publish signed listings (NIP-99, kind 30402, t=hashrate) with a
    price in XBT/TH/day, TH/s, tranche length and a BOLT12 offer;
  * the buyer asks by NIP-17 (`rent_request` with hours and its gateway);
  * the seller accepts and asks for each tranche in advance (`due`);
  * the buyer pays the offer over its wallet connection (NWC) and sends the
    preimage (`paid`); the seller checks it in its own wallet and sends the
    hash to the buyer's gateway;
  * the buyer measures the hash on its own gateway and stops paying when it
    does not arrive. Worst case for the buyer: one tranche.

What is different here: the agent puts a stratum proxy (its "meter") in front
of the buyer's gateway. The panel does not need one: it already measures every
worker on the DATUM gateway (shares x vardiff, audit.py). Each rental gets its
own worker (`<address>.p2h<6 hex>`), so what the seller's rig delivers is
measured exactly, like MRR's and PyBLØCK's rentals.

Money: nothing is paid unless the owner asks for it.
  * A rental only starts when the owner presses Rent and confirms.
  * By default every tranche waits for the owner's click ("Pagar tramo N"),
    within the seller's grace period (a couple of minutes); a Telegram notice
    says when one is due. If nobody pays, the seller sends the rig home.
  * Paying the tranches automatically is possible only when the owner turns it
    on for that rental AND has set both caps (sats per rental and per 24 h),
    which are 0 (off) by default.
  * In both modes a tranche is paid only if: it comes from the seller of the
    running rental, is the next one in order, was never paid, is for the
    listing's offer, asks no more than the listing price (+1 %), stays within
    the hours bought and the caps, and (from the 2nd on) the gateway saw at
    least `min_pct` of the advertised hash since the previous payment.
  * The paying wallet connection should be its own NWC app with a budget (the
    hub enforces it); its URI lives only in config.json (600, gitignored) and
    is never shown back.
"""
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time

import nostr

PROTO = "pyblock-hashrate/0"
DEFAULTS = {
    "enabled": True,
    "relays": ["wss://nostr.pyblock.xyz:8443", "wss://relay.kilombino.com", "wss://nos.lol"],
    "nwc": "",                              # paying wallet connection (its own app with a budget)
    "gateway": {"host": "", "port": 0},     # public stratum of YOUR gateway ("" = pyblock.gateway)
    "address": "",                          # "" = pyblock.address -> rent_address -> MRR profile
    "worker_prefix": "p2h",
    "caps": {"rental_sats": 0, "day_sats": 0},   # 0 = off: no tranche is ever paid automatically
    "min_pct": 0.5,                         # least share of the advertised hash that keeps paying
    "stale_s": 2700,                        # agents renew their listing every 30 min
    "pay_timeout_s": 90,
}
STATE_LABELS = {"requesting": "pidiendo al vendedor", "waiting payment": "esperando el 1er pago",
                "mining": "minando", "ended": "terminado"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RentError(Exception):
    pass


def settings(c):
    p = dict(DEFAULTS)
    p.update(c.get("p2hash") or {})
    p["gateway"] = dict(DEFAULTS["gateway"], **(p.get("gateway") or {}))
    p["caps"] = dict(DEFAULTS["caps"], **(p.get("caps") or {}))
    return p


def work_to_ths(work, secs):
    return work * 2 ** 32 / secs / 1e12 if secs > 0 else 0.0


def ths_to_work(ths, secs):
    return ths * 1e12 * secs / 2 ** 32


def parse_listing(ev, now=None, stale_s=2700):
    """A P2Hash listing in the shape the dashboard shows, or None if it is not one."""
    now = now or time.time()
    if ev.get("kind") != 30402:
        return None
    t = {x[0]: x for x in ev.get("tags") or [] if isinstance(x, list) and x}
    g = lambda k: (t.get(k) or [None, None])[1]   # noqa: E731
    try:
        exp = int(g("expiration") or 0)
        ths, tr = float(g("ths") or 0), int(float(g("tranche_s") or 0))
        price = t.get("price") or []
        xbt = float(price[1]) if len(price) > 1 else 0
    except (TypeError, ValueError):
        return None
    if exp and exp < now:
        return None
    if not (ths > 0 and tr >= 60 and xbt > 0 and len(price) >= 4 and price[2] == "XBT" and price[3] == "day"):
        return None
    sats_day = round(xbt * 1e8)
    offer = g("offer") or ""
    rentable = g("pay") == "bolt12" and offer.startswith("lno1")
    stale = now - int(ev.get("created_at") or 0) > stale_s
    try:
        min_h, max_h = float(g("min_hours") or 1), float(g("max_hours") or 1)
    except ValueError:
        min_h, max_h = 1.0, 1.0
    return {"addr": f"30402:{ev['pubkey']}:{g('d')}", "pubkey": ev["pubkey"], "id": g("d"), "title": (g("title") or "")[:120],
            "status": "stale" if stale else (g("status") or "available"), "ths": ths, "tranche_s": tr,
            "price_sats_th_day": sats_day, "tranche_sats": round(sats_day * ths * tr / 86400),
            "min_h": min_h, "max_h": max_h, "offer": offer, "rentable": rentable, "updated": int(ev.get("created_at") or 0),
            "proto": g("proto") or ""}


def probe_gateway(host, port, user, timeout=6):
    """The check a seller makes before accepting: stratum subscribe + authorize answer."""
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as e:
        return {"ok": False, "why": str(e)[:120]}
    try:
        s.settimeout(timeout)
        s.sendall((json.dumps({"id": 1, "method": "mining.subscribe", "params": ["mrr-command-center-probe"]}) + "\n"
                   + json.dumps({"id": 2, "method": "mining.authorize", "params": [user, "x"]}) + "\n").encode())
        buf, got, t_end = b"", set(), time.time() + timeout
        while time.time() < t_end and got != {1, 2}:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                if j.get("id") == 1 and j.get("result"):
                    got.add(1)
                if j.get("id") == 2 and j.get("result") is True:
                    got.add(2)
        return {"ok": got == {1, 2}, "why": "OK" if got == {1, 2} else "el gateway no respondió a subscribe/authorize"}
    except OSError as e:
        return {"ok": False, "why": str(e)[:120]}
    finally:
        s.close()


class P2Hash:
    def __init__(self, here, cfg_fn, measure=None, notify=None, address_fn=None, analyse=None, on_finish=None,
                 pool_factory=None, wallet_factory=None, clock=time.time):
        self.path = os.path.join(here, "p2hash.json")
        self.cfg_fn = cfg_fn
        # measure(worker, start, end) -> accepted work (difficulty-1 units) seen on the gateway
        self.measure = measure or (lambda w, a, b: 0.0)
        self.notify = notify or (lambda c, text: None)
        self.address_fn = address_fn or (lambda: ("", ""))
        self.analyse = analyse or (lambda *a: None)
        self.on_finish = on_finish or (lambda summary: None)
        self.pool_factory = pool_factory or (lambda urls: nostr.Pool(urls, log=self._log))
        self.wallet_factory = wallet_factory or (lambda uri: nostr.NWC(uri, log=self._log))
        self.now = clock
        self.lock = threading.RLock()
        self.listings = {}
        self.rental = None
        self.pool = None
        self.wallet = None
        self._wallet_uri = None
        self._winfo = {"at": 0}
        self._bud = None
        self.events = []
        self.state = self._load()
        self.sk = self.state["sk"]
        self.pk = nostr.pubkey(self.sk)
        if not os.path.exists(self.path):
            self._save()          # keep the buyer key from the first start on
        if self.state.get("active"):
            # a restart closes a running rental (it is not resumed): tell the seller once connected
            a = self.state.pop("active")
            a.update(status="ended", reason="panel reiniciado", ended=int(self.now()))
            self.state["history"].insert(0, a)
            self.state["cancel_on_start"] = {"seller": a.get("seller"), "rental_id": a.get("rental_id")}
            self._save()

    # -------------------------------------------------------------- persistence (600: holds the buyer key)
    def _load(self):
        try:
            with open(self.path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        if not HEX64.match(str(st.get("sk") or "")):
            st["sk"] = nostr.gen_sk()      # the buyer identity: its own key, never the seller's
        st.setdefault("history", [])
        st.setdefault("payments", [])
        st.setdefault("autopay", False)
        return st

    def _save(self):
        with self.lock:
            st = dict(self.state)
            st["active"] = self.summary(self.rental) if self.rental else None
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(st, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def _log(self, *a):
        print("[p2hash]", *a, flush=True)

    def _event(self, text):
        with self.lock:
            self.events.insert(0, {"at": int(self.now()), "text": text})
            del self.events[40:]
        self._log(text)

    def _tell(self, text):
        try:
            self.notify(self.cfg_fn(), text)
        except Exception:
            pass

    def settings(self):
        return settings(self.cfg_fn())

    def caps(self):
        """Caps in force: the ones set from the dashboard, else config.json's (0 = off)."""
        c = dict(self.settings()["caps"])
        c.update({k: int(v) for k, v in (self.state.get("caps") or {}).items() if v is not None})
        return {"rental_sats": max(0, int(c.get("rental_sats") or 0)), "day_sats": max(0, int(c.get("day_sats") or 0))}

    def autopay_allowed(self):
        c = self.caps()
        return bool(self.state.get("autopay")) and c["rental_sats"] > 0 and c["day_sats"] > 0

    # -------------------------------------------------------------- relays and wallet
    def start(self):
        p = self.settings()
        self.pool = self.pool_factory(p["relays"])
        self.pool.sub("bl", {"kinds": [30402], "#t": ["hashrate"]}, self.on_listing)
        self.pool.sub("bdm", {"kinds": [1059], "#p": [self.pk], "since": int(self.now()) - nostr.TWO_DAYS}, self.on_wrap)
        threading.Thread(target=self._loop, daemon=True, name="p2hash").start()

    def _loop(self):
        while True:
            try:
                c = self.state.get("cancel_on_start")
                if c and self.pool and any(s["open"] for s in self.pool.status()):
                    if c.get("seller") and c.get("rental_id"):
                        self.send(c["seller"], {"type": "cancel", "rental_id": c["rental_id"]})
                    with self.lock:
                        self.state.pop("cancel_on_start", None)
                    self._save()
                self.tick()
                if self.wallet_conn() and self.now() - self._winfo.get("at", 0) > 300 and not (
                        self.rental and self.rental.get("paying")):
                    self.wallet_info(force=True)
            except Exception as e:     # never let the thread die
                self._log("tick failed:", e)
            time.sleep(2)

    def wallet_conn(self):
        uri = (self.settings().get("nwc") or "").strip()
        if not uri:
            return None
        if self.wallet is None or uri != self._wallet_uri:
            if self.wallet:
                self.wallet.close()
            self.wallet, self._wallet_uri, self._winfo, self._bud = self.wallet_factory(uri), uri, {"at": 0}, None
        return self.wallet

    def wallet_info(self, force=False):
        """What the paying connection says about itself (methods, budget). Never the URI."""
        w = self.wallet_conn()
        if not w:
            return {"configured": False}
        if not force and self.now() - self._winfo.get("at", 0) < 300 and self._winfo.get("configured"):
            return self._winfo
        info = {"configured": True, "at": self.now(), "wallet": getattr(w, "wpk", "")[:8]}
        r = w.request("get_info", {}, 20)
        if r.get("error"):
            info["error"] = r["error"].get("message") or r["error"].get("code")
        else:
            res = r.get("result") or {}
            m = res.get("methods") or []
            info.update(methods=m, alias=res.get("alias"), pay_method=("pay_offer" if "pay_offer" in m else
                                                                        "pay" if "pay" in m else None))
            if not info["pay_method"]:
                info["error"] = "esta conexión no puede pagar ofertas BOLT12 (falta pay_offer o pay)"
        self._winfo = info
        self.budget(force=True)
        return info

    def budget(self, force=False):
        """{left, total} in sats of what the paying connection may still spend (None = it does not say)."""
        w = self.wallet_conn()
        if not w:
            return None
        if not force and self._bud and self.now() - self._bud["at"] < 60:
            return self._bud
        m = self._winfo.get("methods") or []
        out = {"at": self.now(), "left": None, "total": None, "renews": None, "source": None}
        if "get_budget" in m:
            r = (w.request("get_budget", {}, 15).get("result") or {})
            tot = int(r.get("total_budget") or 0)
            if tot > 0:
                out.update(total=tot // 1000, left=max(0, (tot - int(r.get("used_budget") or 0)) // 1000),
                           renews=r.get("renews_at"), source="get_budget")
        elif "get_balance" in m:
            r = w.request("get_balance", {}, 15).get("result") or {}
            if r.get("balance") is not None:
                out.update(left=int(r["balance"]) // 1000, source="get_balance")
        self._bud = out
        return out

    # -------------------------------------------------------------- listings
    def on_listing(self, ev, url):
        if ev.get("kind") != 30402 or ev.get("pubkey") == self.pk or not nostr.verify_event(ev):
            return
        d = nostr.tag(ev, "d")
        if not d:
            return
        key = f"30402:{ev['pubkey']}:{d}"
        with self.lock:
            cur = self.listings.get(key)
            if not cur or ev["created_at"] > cur["ev"]["created_at"]:
                self.listings[key] = {"ev": ev, "relays": set(cur["relays"]) if cur else set()}
            self.listings[key]["relays"].add(url)

    def market(self):
        now, stale = self.now(), self.settings()["stale_s"]
        with self.lock:
            items = list(self.listings.values())
        out = []
        for it in items:
            L = parse_listing(it["ev"], now, stale)
            if L:
                L["relays"] = len(it["relays"])
                out.append(L)
        out.sort(key=lambda x: (x["status"] != "available", not x["rentable"], x["price_sats_th_day"]))
        return out

    # -------------------------------------------------------------- money bookkeeping
    def spent24h(self):
        t = self.now() - 86400
        return sum(p["sats"] for p in self.state["payments"] if p["at"] >= t)

    def quote(self, L, hours, auto=False):
        n = -(-int(round(hours * 3600)) // int(L["tranche_s"]))
        total = n * L["tranche_sats"]
        caps = self.caps()
        why = None
        if not (L["min_h"] <= hours <= L["max_h"]):
            why = f"este rig se alquila de {L['min_h']:g} a {L['max_h']:g} horas"
        elif caps["rental_sats"] and total > caps["rental_sats"]:
            why = f"cuesta {total:,} sats, más que tu tope de {caps['rental_sats']:,} por alquiler"
        elif caps["day_sats"] and self.spent24h() + total > caps["day_sats"]:
            why = (f"con los {self.spent24h():,} sats de las últimas 24 h pasas tu tope de "
                   f"{caps['day_sats']:,} por día")
        elif auto and not self.autopay_allowed():
            why = "el pago automático está apagado o sin topes (los dos topes > 0)"
        bud = self._bud
        per = L["tranche_sats"] + max(10, -(-L["tranche_sats"] // 100))
        if not why and bud and bud.get("left") is not None and bud["left"] < n * per:
            why = (f"cuesta hasta {total:,} sats más comisiones, pero a la conexión de pago le quedan "
                   f"{bud['left']:,} sats: da para {bud['left'] // per} de {n} tramos")
        return {"tranches": n, "total": total, "why": why, "per_tranche": L["tranche_sats"]}

    # -------------------------------------------------------------- renting
    def gateway(self):
        p, c = self.settings(), self.cfg_fn()
        gw = p["gateway"] if p["gateway"].get("host") else ((c.get("pyblock") or {}).get("gateway") or {})
        return (gw.get("host") or ""), int(gw.get("port") or 0)

    def rent(self, addr, hours, auto=False):
        p = self.settings()
        if not p.get("enabled", True):
            raise RentError("P2Hash está desactivado (p2hash.enabled)")
        if not self.wallet_conn():
            raise RentError("falta la conexión de pago (NWC): pégala en la caja P2Hash")
        host, port = self.gateway()
        if not host or not port:
            raise RentError("falta el gateway público (p2hash.gateway o pyblock.gateway en config.json)")
        address, _ = self.address_fn()
        if not address:
            raise RentError("no hay address de cobro (rent_address o perfil de MRR)")
        with self.lock:
            if self.rental:
                raise RentError("ya hay un alquiler P2Hash en marcha: uno cada vez")
        L = next((x for x in self.market() if x["addr"] == addr), None)
        if not L:
            raise RentError("ese anuncio ya no está (caducado o nunca visto)")
        if not L["rentable"]:
            raise RentError("ese anuncio no acepta pagos BOLT12 de agentes")
        if L["status"] != "available":
            raise RentError(f"el rig está {L['status']}")
        info = self.wallet_info()
        if info.get("error"):
            raise RentError("conexión de pago: " + info["error"])
        self.budget(force=True)
        q = self.quote(L, float(hours), auto)
        if q["why"]:
            raise RentError(q["why"])
        worker = f"{p['worker_prefix']}{secrets.token_hex(3)}"
        req = secrets.token_hex(6)
        now = int(self.now())
        r = {"req": req, "rental_id": None, "seller": L["pubkey"], "listing": addr, "title": L["title"], "ths": L["ths"],
             "hours": float(hours), "tranche_s": L["tranche_s"], "tranche_sats": L["tranche_sats"],
             "max_tranche": round(L["tranche_sats"] * 1.01) + 1, "offer": L["offer"], "n_max": q["tranches"],
             "cap": q["total"], "auto": bool(auto), "status": "requesting", "reason": None, "asked": now,
             "started": None, "paid": [], "tranches": [], "refused": [], "pending": None,
             "worker": worker, "address": address, "gateway": f"{host}:{port}",
             "price_sats_th_day": L["price_sats_th_day"], "early": []}
        with self.lock:
            if self.rental:
                raise RentError("ya hay un alquiler P2Hash en marcha: uno cada vez")
            self.rental = r
        self._save()
        self.send(L["pubkey"], {"type": "rent_request", "req": req, "listing": addr, "hours": float(hours),
                                "gateway": {"host": host, "port": port, "user": f"{address}.{worker}"}})
        self._event(f"pedido a {L['title'] or L['id']}: {hours:g} h, {q['tranches']} tramos × {L['tranche_sats']:,} sats"
                    f" ({'pago automático' if auto else 'confirmas cada tramo'})")
        return self.summary(r)

    def send(self, to, obj):
        if not self.pool:
            return []
        w = nostr.wrap_dm(self.sk, to, json.dumps(dict({"proto": PROTO}, **obj)))
        return self.pool.publish(w)

    def stop(self, why="parado por ti"):
        with self.lock:
            r = self.rental
        if not r:
            return False
        if r.get("rental_id"):
            self.send(r["seller"], {"type": "cancel", "rental_id": r["rental_id"]})
        self.close(why)
        return True

    # -------------------------------------------------------------- messages from the seller
    def on_wrap(self, ev, _url):
        got = nostr.unwrap_dm(ev, self.sk)
        if not got:
            return
        sender, rumor = got
        try:
            msg = json.loads(rumor.get("content") or "")
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("proto") != PROTO:
            return
        at = int(rumor.get("created_at") or 0)
        with self.lock:
            r = self.rental
            if not r or sender != r["seller"] or at < r["asked"] - 120:
                return        # only the seller of the running rental, nothing older than it
            typ = msg.get("type")
            if typ == "rent_reject" and msg.get("req") == r["req"] and r["status"] == "requesting":
                reason = "rechazado: " + str(msg.get("reason") or "")[:120]
            elif typ == "rent_accept" and msg.get("req") == r["req"] and r["status"] == "requesting":
                reason = None
                try:
                    bad = (int(-(-float(msg.get("tranche_s") or 0) // 1)) != r["tranche_s"]
                           or (msg.get("tranche_sats") and float(msg["tranche_sats"]) > r["max_tranche"]))
                except (TypeError, ValueError):
                    bad = True
                r["rental_id"] = str(msg.get("rental_id") or "")
                if bad:
                    reason = "refuse:la aceptación no coincide con el anuncio (tramo o precio)"
                else:
                    r["status"] = "waiting payment"
                    early, r["early"] = r["early"], []
            elif not r.get("rental_id"):
                if msg.get("rental_id") and len(r["early"]) < 20:
                    r["early"].append(msg)     # relays do not keep the order: replay after the acceptance
                return
            elif msg.get("rental_id") != r["rental_id"]:
                return
            else:
                reason = "msg"
        if typ == "rent_reject":
            return self.close(reason, from_seller=True)
        if typ == "rent_accept":
            if reason:
                return self.refuse(reason[7:])
            self._event(f"aceptado por el vendedor (alquiler {r['rental_id'][:12]})")
            self._tell(f"🟪 P2Hash: aceptado — {r['title']} · {r['ths']:g} TH/s × {r['hours']:g} h · "
                       f"{r['n_max']} tramos × {r['tranche_sats']:,} sats")
            self._save()
            for m in early:
                self._on_msg(m)
            return
        self._on_msg(msg)

    def _on_msg(self, msg):
        typ = msg.get("type")
        if typ == "due":
            return self.on_due(msg)
        if typ == "tranche":
            return self.on_tranche(msg)
        if typ == "rent_end":
            return self.close(str(msg.get("reason") or "terminado por el vendedor")[:80], from_seller=True)

    def _check_due(self, r, n, sats, offer):
        """None if the tranche may be paid, else the reason not to."""
        if n != len(r["paid"]) + 1:
            return f"tramo {n} pedido fuera de orden (toca el {len(r['paid']) + 1})"
        if n > r["n_max"]:
            return f"tramo {n} pedido, pero compraste {r['n_max']}"
        if offer != r["offer"]:
            return "el cobro no es para la oferta del anuncio"
        if not (sats > 0) or sats > r["max_tranche"]:
            return f"piden {sats} sats, el anuncio dice {r['tranche_sats']}"
        caps = self.caps()
        spent = sum(p["sats"] for p in r["paid"])
        if caps["rental_sats"] and spent + sats > caps["rental_sats"]:
            return f"pasa tu tope de {caps['rental_sats']:,} sats por alquiler"
        if caps["day_sats"] and self.spent24h() + sats > caps["day_sats"]:
            return f"pasa tu tope de {caps['day_sats']:,} sats por día"
        if n > 1:   # own measurement: what reached OUR gateway since the previous payment
            last = r["paid"][-1]["at"]
            secs = max(1, self.now() - last)
            got = self.measure(r["worker"], last, self.now())
            want = ths_to_work(r["ths"], min(secs, r["tranche_s"]))
            if got < self.settings()["min_pct"] * want:
                return (f"tu gateway midió {work_to_ths(got, secs):.2f} TH/s desde el último pago y el rig anuncia "
                        f"{r['ths']:g}")
        return None

    def on_due(self, msg):
        try:
            n, sats = int(msg.get("n")), int(msg.get("sats"))
        except (TypeError, ValueError):
            return self.refuse("cobro ilegible")
        with self.lock:
            r = self.rental
            if not r or r.get("paying") or any(p["n"] == n for p in r["paid"]):
                return            # repeated message
            if r["auto"] and not self.autopay_allowed():
                r["auto"] = False        # switched off (or caps removed) meanwhile: ask the owner instead
            why = self._check_due(r, n, sats, msg.get("offer"))
            if why:
                pass
            elif r["auto"]:
                r["paying"] = True
            else:
                due_s = int(msg.get("due_s") or 120)
                r["pending"] = {"n": n, "sats": sats, "at": int(self.now()), "due_s": due_s}
        if why:
            return self.refuse(why)
        if r["auto"]:
            return self._pay(r, n, sats)
        self._save()
        self._event(f"tramo {n}: {sats:,} sats pendientes de tu confirmación")
        self._tell(f"⏳ P2Hash: tramo {n} de {r['n_max']} ({sats:,} sats) espera tu confirmación en el panel "
                   f"(el vendedor espera ~{r['pending']['due_s']} s)")

    def confirm(self, n):
        """The owner pressed "Pagar tramo n": the same checks again (fresh measure), then pay."""
        with self.lock:
            r = self.rental
            if not r or not r.get("pending") or r["pending"]["n"] != int(n):
                raise RentError("no hay ningún tramo pendiente con ese número")
            if r.get("paying"):
                raise RentError("ya se está pagando")
            pend = r["pending"]
            why = self._check_due(r, pend["n"], pend["sats"], r["offer"])
            if not why:
                r["paying"] = True
        if why:
            self.refuse(why)
            raise RentError("no se paga: " + why)
        return self._pay(r, pend["n"], pend["sats"])

    def _pay(self, r, n, sats):
        w = self.wallet_conn()
        method = (self._winfo.get("pay_method") or "pay_offer") if w else None
        res = {"error": {"code": "NO_WALLET", "message": "sin conexión de pago"}}
        if w:
            note = f"p2hash {r['rental_id']} #{n}"
            params = ({"offer": r["offer"], "amount": sats * 1000, "payer_note": note} if method == "pay_offer"
                      else {"payment": r["offer"], "amount": sats * 1000, "payer_note": note})
            res = w.request(method, params, int(self.settings()["pay_timeout_s"]))
        pre = str((res.get("result") or {}).get("preimage") or "").lower()
        with self.lock:
            r["paying"] = False
            if self.rental is not r:
                return {"ok": False}
            if not HEX64.match(pre):
                err = res.get("error") or {}
                if err.get("code") == "TIMEOUT" or err.get("code") == "INTERNAL":
                    # no answer is not "not paid": count it against the caps, conservatively
                    self.state["payments"].append({"at": int(self.now()), "sats": sats, "doubtful": True})
                    r["refused"].append({"at": int(self.now()), "why": f"tramo {n}: la billetera no contestó a tiempo; "
                                         "contado en los topes por si salió"})
                why = "tu billetera no pagó: " + str(err.get("message") or err.get("code") or "sin preimagen")[:160]
            else:
                why = None
                ph = str((res.get("result") or {}).get("payment_hash") or "").lower()
                if HEX64.match(ph) and hashlib.sha256(bytes.fromhex(pre)).hexdigest() != ph:
                    why = "la preimagen no corresponde al pago"
        if why:
            self.budget(force=True)
            self.refuse(why)
            return {"ok": False, "error": why}
        fee = int((res.get("result") or {}).get("fees_paid") or 0)
        with self.lock:
            pay = {"n": n, "sats": sats, "fee_msat": fee, "at": int(self.now())}
            r["paid"].append(pay)
            r["pending"] = None
            self.state["payments"].append({"at": pay["at"], "sats": sats})
            self.state["payments"] = [p for p in self.state["payments"] if p["at"] >= self.now() - 2 * 86400]
            if not r["started"]:
                r["started"], r["status"] = pay["at"], "mining"
        self._save()
        self.send(r["seller"], {"type": "paid", "rental_id": r["rental_id"], "n": n, "preimage": pre})
        self._event(f"tramo {n} pagado: {sats:,} sats (comisión {fee / 1000:g})")
        if n == 1:
            self._tell(f"⛏ P2Hash: primer tramo pagado, el rig viene a tu gateway — {r['title']} ({r['worker']})")
        self.budget(force=True)
        return {"ok": True, "paid": pay}

    def on_tranche(self, msg):
        with self.lock:
            r = self.rental
            if not r:
                return
            t = int(self.now())
            frm = r.get("lastT") or r.get("started") or t
            secs = max(1, t - frm)
            got = self.measure(r["worker"], frm, t)
            try:
                n, shares = int(msg.get("n") or 0), int(msg.get("shares") or 0)
            except (TypeError, ValueError):
                n, shares = 0, 0
            r["tranches"].append({"n": n, "seller_shares": shares, "ths": round(work_to_ths(got, secs), 3),
                                  "secs": secs, "at": t})
            del r["tranches"][:-300]
            r["lastT"] = t
        self._save()

    def refuse(self, why):
        with self.lock:
            r = self.rental
            if not r:
                return
            r["refused"].append({"at": int(self.now()), "why": why})
        self._event("NO SE PAGA: " + why)
        if r.get("rental_id"):
            self.send(r["seller"], {"type": "cancel", "rental_id": r["rental_id"]})
        self.close("no pagado: " + why)

    def close(self, reason, from_seller=False):
        with self.lock:
            r = self.rental
            if not r:
                return
            self.rental = None
            s = self.summary(r, reason)
            self.state["history"].insert(0, s)
            del self.state["history"][30:]
        self._save()
        self._event(f"alquiler cerrado: {reason}")
        self._tell(f"🏁 P2Hash: {r['title']} — {reason} · {s['paid']} tramos, {s['sats']:,} sats"
                   + (f" · gateway {s['analysis']['delivered_th']:.2f} TH/s" if s.get("analysis") else ""))
        if r.get("started"):
            try:
                self.on_finish(s)
            except Exception as e:
                self._log("history failed:", e)

    def summary(self, r, reason=None):
        now = int(self.now())
        end = now if reason else None
        sats = sum(p["sats"] for p in r["paid"])
        fees = sum(p["fee_msat"] for p in r["paid"]) / 1000
        ends = r["started"] + r["hours"] * 3600 if r.get("started") else None
        out = {k: r.get(k) for k in ("rental_id", "seller", "listing", "title", "ths", "hours", "tranche_s", "tranche_sats",
                                     "n_max", "cap", "auto", "asked", "started", "worker", "address", "gateway",
                                     "price_sats_th_day", "refused", "tranches", "pending")}
        out.update(status="ended" if reason else r["status"], reason=reason, ended=end, paid=len(r["paid"]),
                   sats=sats, fees_sats=round(fees, 3), ends=ends,
                   pays=[{"n": p["n"], "at": p["at"], "sats": p["sats"]} for p in r["paid"]],
                   next_due=(r["paid"][-1]["at"] + r["tranche_s"]) if r["paid"] and len(r["paid"]) < r["n_max"] else None)
        out["status_label"] = STATE_LABELS.get(out["status"], out["status"])
        out["analysis"] = None
        if r.get("started"):
            try:
                out["analysis"] = self.analyse(r["worker"], r["started"], min(ends, end or now) if ends else now, r["ths"])
            except Exception:
                out["analysis"] = None
        return out

    def tick(self):
        with self.lock:
            r = self.rental
        if not r:
            return
        t = self.now()
        if r["status"] == "requesting" and t - r["asked"] > 120:
            return self.close("el vendedor no contestó en 2 minutos")
        if r["status"] == "waiting payment" and not r.get("pending") and t - r["asked"] > 600:
            return self.stop("el vendedor nunca pidió el primer pago")
        pend = r.get("pending")
        if pend and not r.get("paying") and t - pend["at"] > pend["due_s"] + 60:
            return self.stop(f"tramo {pend['n']} sin confirmar a tiempo: el vendedor se lleva el rig")
        if r.get("started") and t > r["started"] + r["hours"] * 3600 + 3 * r["tranche_s"] + 300:
            return self.close("el vendedor no cerró el alquiler: cerrado aquí")

    # -------------------------------------------------------------- settings from the dashboard
    def set_wallet(self, uri, write_config):
        """Save (or clear with "") the paying connection URI in config.json. Returns the info view."""
        uri = str(uri or "").strip()
        if uri:
            nostr.parse_nwc(uri)            # ValueError when it is not one
        write_config(uri)
        self._winfo, self._bud = {"at": 0}, None
        return self.wallet_info(force=True) if uri else {"configured": False}

    def set_caps(self, rental_sats=None, day_sats=None):
        caps = dict(self.state.get("caps") or {})
        for k, v in (("rental_sats", rental_sats), ("day_sats", day_sats)):
            if v is not None:
                v = int(v)
                if v < 0 or v > 10 ** 10:
                    raise RentError("tope fuera de rango")
                caps[k] = v
        with self.lock:
            self.state["caps"] = caps
            if not self.autopay_allowed():
                self.state["autopay"] = False
        self._save()
        return self.caps()

    def set_autopay(self, on):
        with self.lock:
            if on:
                c = self.caps()
                if not (c["rental_sats"] > 0 and c["day_sats"] > 0):
                    raise RentError("pon antes los dos topes (por alquiler y por 24 h)")
            self.state["autopay"] = bool(on)
            r = self.rental
            if r and not on:
                r["auto"] = False       # the running rental falls back to confirming each tranche
        self._save()
        return self.state["autopay"]

    def forget(self, idx):
        with self.lock:
            if not 0 <= int(idx) < len(self.state["history"]):
                raise RentError("no existe")
            self.state["history"].pop(int(idx))
        self._save()

    # -------------------------------------------------------------- view
    def view(self):
        p = self.settings()
        host, port = self.gateway()
        address, src = self.address_fn()
        with self.lock:
            r = self.rental
            active = self.summary(r) if r else None
            hist = [dict(h) for h in self.state["history"][:15]]
            events = list(self.events[:15])
        if active:
            active["gateway_th"] = None
        for h in hist:
            h.pop("tranches", None)
        return {
            "enabled": bool(p.get("enabled", True)), "buyer": self.pk[:12],
            "relays": self.pool.status() if self.pool else [],
            "wallet": {k: v for k, v in self._winfo.items() if k != "at"} if self.wallet_conn() else {"configured": False},
            "budget": ({k: v for k, v in self._bud.items() if k != "at"} if self._bud else None),
            "gateway": f"{host}:{port}" if host else "", "address": address, "address_source": src,
            "worker_prefix": p["worker_prefix"], "caps": self.caps(), "autopay": bool(self.state.get("autopay")),
            "autopay_allowed": self.autopay_allowed(), "spent_24h": self.spent24h(), "min_pct": p["min_pct"],
            "listings": [dict(L, offer=bool(L["offer"])) for L in self.market()],
            "active": active, "history": hist, "events": events,
        }
