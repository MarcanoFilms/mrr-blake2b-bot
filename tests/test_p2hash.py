"""P2Hash: the Nostr pieces against nostr-tools vectors, and the renter against a fake seller.

Run: python3 -m unittest discover -s tests
Nothing here talks to a relay or a wallet: the relays are an in-memory pool that
hands every published event to a fake seller that follows the PyBLØCK agent's
protocol (rent_accept, due, tranche, rent_end), and the wallet is a fake NWC.
The keys in the vectors are test keys (one of them is the number 3).
"""
import json
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import nostr  # noqa: E402
import p2hash  # noqa: E402

with open(os.path.join(HERE, "fixtures", "nostr_vectors.json")) as f:
    V = json.load(f)
FAKE_ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"      # BIP-173 test vector, not ours
OFFER = "lno1qqfakeofferfortestsonly"
PRE = "ab" * 32


class NostrVectors(unittest.TestCase):
    def test_keys_and_conversation(self):
        self.assertEqual(nostr.pubkey(V["skA"]), V["pkA"])
        self.assertEqual(nostr.pubkey(V["skB"]), V["pkB"])
        self.assertEqual(nostr.conversation_key(V["skA"], V["pkB"]).hex(), V["ck"])
        self.assertEqual(nostr.conversation_key(V["skB"], V["pkA"]).hex(), V["ck"])

    def test_nip44(self):
        ck = bytes.fromhex(V["ck"])
        for e in V["enc"]:
            self.assertEqual(nostr.nip44_encrypt(e["m"], ck, bytes.fromhex(V["nonce"])), e["c"])
            self.assertEqual(nostr.nip44_decrypt(e["c"], ck), e["m"])
        bad = V["enc"][0]["c"][:-6] + ("A" if V["enc"][0]["c"][-6] != "A" else "B") + V["enc"][0]["c"][-5:]
        with self.assertRaises(ValueError):
            nostr.nip44_decrypt(bad, ck)
        self.assertEqual(nostr._padded_len(1), 32)
        self.assertEqual(nostr._padded_len(33), 64)
        self.assertEqual(nostr._padded_len(257), 320)

    def test_events(self):
        self.assertTrue(nostr.verify_event(V["ev"]))                     # signed by nostr-tools
        bad = dict(V["ev"], content=V["ev"]["content"] + "!")
        self.assertFalse(nostr.verify_event(bad))
        ev = nostr.finalize({"kind": 1, "created_at": 1, "tags": [["t", "x"]], "content": 'a "b"\n ñ'}, V["skB"])
        self.assertTrue(nostr.verify_event(ev))
        self.assertEqual(ev["pubkey"], V["pkB"])

    def test_gift_wrap(self):
        got = nostr.unwrap_dm(V["wrap"], V["skB"])                       # wrapped by nostr-tools
        self.assertEqual(got[0], V["pkA"])
        self.assertEqual(json.loads(got[1]["content"])["type"], "rent_accept")
        self.assertIsNone(nostr.unwrap_dm(V["wrap"], V["skA"]))          # not for A
        w = nostr.wrap_dm(V["skA"], V["pkB"], "hola")
        self.assertEqual(w["kind"], 1059)
        self.assertNotEqual(w["pubkey"], V["pkA"])                        # ephemeral key outside
        self.assertEqual(nostr.unwrap_dm(w, V["skB"])[1]["content"], "hola")

    def test_nwc_uri(self):
        u = f"nostr+walletconnect://{V['pkA']}?relay=wss%3A%2F%2Frelay.example.org&secret={V['skB']}"
        c = nostr.parse_nwc(u)
        self.assertEqual(c["wallet_pk"], V["pkA"])
        self.assertEqual(c["relays"], ["wss://relay.example.org"])
        for bad in ("", OFFER, "nostr+walletconnect://abc?relay=wss://x&secret=00", "lnurl1xyz"):
            with self.assertRaises(ValueError):
                nostr.parse_nwc(bad)


# ----------------------------------------------------------------------------- fakes
class FakePool:
    """In-memory relays: what the renter publishes goes to the seller; what the seller
    sends goes to the renter's subscriptions."""

    def __init__(self):
        self.subs = {}
        self.seller = None
        self.sent = []

    def sub(self, sid, flt, cb):
        self.subs[sid] = cb

    def publish(self, ev):
        self.sent.append(ev)
        if self.seller:
            self.seller.receive(ev)
        return [("wss://fake", True, "")]

    def deliver(self, ev):
        for sid, cb in self.subs.items():
            if sid == "bdm" and ev["kind"] == 1059:
                cb(ev, "wss://fake")
            if sid == "bl" and ev["kind"] == 30402:
                cb(ev, "wss://fake")

    def status(self):
        return [{"url": "wss://fake", "open": True, "error": None}]


class Seller:
    """A seller agent: answers rent_request, asks for tranches, checks nothing (the
    tests drive it by hand)."""

    def __init__(self, pool, buyer_pk, ths=10.0, tranche_s=600, price_sats=1_000_000, offer=OFFER):
        self.sk = V["skA"]
        self.pk = V["pkA"]
        self.pool, self.buyer_pk = pool, buyer_pk
        self.ths, self.tranche_s, self.price, self.offer = ths, tranche_s, price_sats, offer
        self.inbox = []
        pool.seller = self

    def listing(self, now, status="available", **over):
        tags = [["d", "rig-1"], ["title", "Test rig"], ["t", "hashrate"], ["t", "blake2b"],
                ["price", f"{self.price / 1e8:.8f}", "XBT", "day"], ["ths", str(self.ths)], ["min_hours", "1"],
                ["max_hours", "24"], ["tranche_s", str(self.tranche_s)], ["status", status],
                ["proto", p2hash.PROTO], ["expiration", str(int(now) + 7200)], ["pay", "bolt12"], ["offer", self.offer]]
        for k, v in over.items():
            tags = [t for t in tags if t[0] != k] + ([[k, *v]] if v is not None else [])
        return nostr.finalize({"kind": 30402, "created_at": int(now), "tags": tags, "content": "test"}, self.sk)

    @property
    def tranche_sats(self):
        return round(self.price * self.ths * self.tranche_s / 86400)

    def receive(self, ev):
        got = nostr.unwrap_dm(ev, self.sk)
        if got:
            self.inbox.append(json.loads(got[1]["content"]))

    def say(self, **msg):
        self.pool.deliver(nostr.wrap_dm(self.sk, self.buyer_pk, json.dumps(dict({"proto": p2hash.PROTO}, **msg))))


class FakeWallet:
    def __init__(self, methods=("get_info", "pay_offer", "get_budget")):
        self.wpk = "f" * 64
        self.methods = list(methods)
        self.calls = []
        self.fail = None
        self.budget_left_msat = 10_000_000_000

    def request(self, method, params=None, timeout=60):
        self.calls.append((method, params))
        if method == "get_info":
            return {"result": {"methods": self.methods, "alias": "test"}}
        if method == "get_budget":
            return {"result": {"total_budget": 10_000_000_000, "used_budget": 10_000_000_000 - self.budget_left_msat}}
        if method in ("pay_offer", "pay"):
            if self.fail:
                return {"error": {"code": self.fail, "message": "nope"}}
            self.budget_left_msat -= params["amount"]
            return {"result": {"preimage": PRE, "fees_paid": 1000}}
        return {"error": {"code": "NOT_IMPLEMENTED"}}

    def close(self):
        pass


class Clock:
    def __init__(self, t=None):
        import time
        self.t = int(t or time.time())   # real time: the seller's messages carry the real time

    def __call__(self):
        return self.t


class RenterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.notices = []
        self.work = {}            # worker -> work returned by measure
        self.conf = {"rent_address": FAKE_ADDR,
                     "p2hash": {"nwc": "nostr+walletconnect://x", "gateway": {"host": "gw.example.com", "port": 23334}}}
        self.pool = FakePool()
        self.wallet = FakeWallet()
        self.finished = []
        self.p = p2hash.P2Hash(self.dir, lambda: self.conf, measure=lambda w, a, b: self.work.get(w, 0.0),
                               notify=lambda c, t: self.notices.append(t), address_fn=lambda: (FAKE_ADDR, "config"),
                               analyse=lambda w, s, e, th: {"delivered_th": 9.9, "pct": 99.0, "verdict": "ok", "worker": w},
                               on_finish=self.finished.append, pool_factory=lambda urls: self.pool,
                               wallet_factory=lambda uri: self.wallet, clock=self.clock)
        self.p.pool = self.pool
        self.p.pool.sub("bl", {}, self.p.on_listing)
        self.p.pool.sub("bdm", {}, self.p.on_wrap)
        self.seller = Seller(self.pool, self.p.pk)
        self.pool.deliver(self.seller.listing(self.clock()))
        self.addr = f"30402:{self.seller.pk}:rig-1"

    def last(self, typ):
        return [m for m in self.seller.inbox if m["type"] == typ][-1]

    def start_rental(self, hours=1, auto=False):
        r = self.p.rent(self.addr, hours, auto)
        req = self.last("rent_request")
        self.assertEqual(req["gateway"]["user"], f"{FAKE_ADDR}.{r['worker']}")
        self.assertEqual(req["gateway"]["host"], "gw.example.com")
        self.seller.say(type="rent_accept", req=req["req"], rental_id="R1", hours=hours,
                        tranche_s=self.seller.tranche_s, tranche_sats=self.seller.tranche_sats, prepay=True)
        return r["worker"]

    def test_listings(self):
        m = self.p.market()
        self.assertEqual(len(m), 1)
        L = m[0]
        self.assertTrue(L["rentable"])
        self.assertEqual(L["price_sats_th_day"], 1_000_000)
        self.assertEqual(L["tranche_sats"], self.seller.tranche_sats)
        # not a P2Hash listing (Lightning Stream style price), expired, stale, own key, bad signature
        t = self.clock()
        self.pool.deliver(nostr.finalize({"kind": 30402, "created_at": t, "content": "", "tags": [
            ["d", "x"], ["t", "hashrate"], ["price", "389", "SAT", "slice"]]}, V["skB"]))
        exp = self.seller.listing(t, d=["rig-2"], expiration=[str(t - 1)])
        self.pool.deliver(exp)
        stale = self.seller.listing(t - 3000, d=["rig-3"])
        self.pool.deliver(stale)
        forged = dict(self.seller.listing(t, d=["rig-4"]), content="changed")
        self.pool.deliver(forged)
        m = {x["id"]: x for x in self.p.market()}
        self.assertEqual(set(m), {"rig-1", "rig-3"})
        self.assertEqual(m["rig-3"]["status"], "stale")
        # a newer listing of the same rig replaces the old one
        self.pool.deliver(self.seller.listing(t + 5, status="rented"))
        self.assertEqual({x["id"]: x for x in self.p.market()}["rig-1"]["status"], "rented")

    def test_manual_tranches(self):
        w = self.start_rental()
        self.assertEqual(self.p.rental["status"], "waiting payment")
        n_max = self.p.rental["n_max"]
        self.assertEqual(n_max, 6)
        self.seller.say(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        # nothing paid until the owner confirms
        self.assertFalse([c for c in self.wallet.calls if c[0] == "pay_offer"])
        self.assertEqual(self.p.rental["pending"]["n"], 1)
        self.assertTrue(any("espera tu confirmación" in t for t in self.notices))
        res = self.p.confirm(1)
        self.assertTrue(res["ok"])
        pay = [c for c in self.wallet.calls if c[0] == "pay_offer"][-1][1]
        self.assertEqual(pay["offer"], OFFER)
        self.assertEqual(pay["amount"], self.seller.tranche_sats * 1000)
        self.assertEqual(self.last("paid"), {"proto": p2hash.PROTO, "type": "paid", "rental_id": "R1", "n": 1, "preimage": PRE})
        self.assertEqual(self.p.rental["status"], "mining")
        # 2nd tranche: the gateway saw the hash (10 TH/s for 600 s)
        self.clock.t += 600
        self.work[w] = p2hash.ths_to_work(9.0, 600)
        self.seller.say(type="tranche", rental_id="R1", n=1, shares=100)
        self.seller.say(type="due", rental_id="R1", n=2, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.assertTrue(self.p.confirm(2)["ok"])
        self.assertEqual(len(self.p.rental["paid"]), 2)
        self.assertEqual(self.p.rental["tranches"][0]["seller_shares"], 100)
        # 3rd: nothing more arrived -> not paid, cancelled, closed
        self.clock.t += 600
        self.work[w] = 0.0
        self.seller.say(type="due", rental_id="R1", n=3, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.assertIsNone(self.p.rental)
        self.assertEqual(self.last("cancel")["rental_id"], "R1")
        h = self.p.state["history"][0]
        self.assertIn("tu gateway midió", h["reason"])
        self.assertEqual(h["paid"], 2)
        self.assertEqual(self.finished[0]["worker"], w)
        self.assertEqual(self.p.spent24h(), 2 * self.seller.tranche_sats)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "p2hash.json")).st_mode), 0o600)

    def test_checks_refuse(self):
        cases = [
            (dict(n=2), "fuera de orden"),
            (dict(offer="lno1qqother"), "oferta del anuncio"),
            (dict(sats=10 ** 9), "el anuncio dice"),
        ]
        for over, text in cases:
            self.setUp()
            self.start_rental()
            msg = dict(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
            msg.update(over)
            self.seller.say(**msg)
            self.assertIsNone(self.p.rental, text)
            self.assertIn(text, self.p.state["history"][0]["reason"])
            self.assertFalse([c for c in self.wallet.calls if c[0] == "pay_offer"])

    def test_messages_from_others_ignored(self):
        self.start_rental()
        other = nostr.wrap_dm(V["skB"], self.p.pk, json.dumps({"proto": p2hash.PROTO, "type": "rent_end",
                                                                 "rental_id": "R1", "reason": "fake"}))
        self.pool.deliver(other)
        self.assertIsNotNone(self.p.rental)
        self.seller.say(type="rent_end", rental_id="R1", reason="completed")
        self.assertIsNone(self.p.rental)

    def test_accept_mismatch_refused(self):
        self.p.rent(self.addr, 1)
        req = self.last("rent_request")
        self.seller.say(type="rent_accept", req=req["req"], rental_id="R1", hours=1, tranche_s=600,
                        tranche_sats=self.seller.tranche_sats * 2)
        self.assertIsNone(self.p.rental)
        self.assertIn("no coincide", self.p.state["history"][0]["reason"])

    def test_autopay_off_by_default(self):
        self.assertFalse(self.p.autopay_allowed())
        with self.assertRaises(p2hash.RentError):
            self.p.rent(self.addr, 1, auto=True)
        with self.assertRaises(p2hash.RentError):
            self.p.set_autopay(True)                    # no caps yet
        self.p.set_caps(rental_sats=self.seller.tranche_sats * 3, day_sats=self.seller.tranche_sats * 10)
        self.p.set_autopay(True)
        # 6 tranches > cap of 3 per rental
        with self.assertRaises(p2hash.RentError) as e:
            self.p.rent(self.addr, 1, auto=True)
        self.assertIn("tope", str(e.exception))
        self.p.set_caps(rental_sats=self.seller.tranche_sats * 6)
        w = self.start_rental(auto=True)
        self.seller.say(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.assertEqual(len(self.p.rental["paid"]), 1)                  # paid by itself
        self.assertIsNone(self.p.rental["pending"])
        # switched off mid-rental: the next one waits for the owner
        self.p.set_autopay(False)
        self.clock.t += 600
        self.work[w] = p2hash.ths_to_work(10.0, 600)
        self.seller.say(type="due", rental_id="R1", n=2, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.assertEqual(len(self.p.rental["paid"]), 1)
        self.assertEqual(self.p.rental["pending"]["n"], 2)

    def test_day_cap_and_doubtful_payment(self):
        self.p.set_caps(rental_sats=10 ** 8, day_sats=self.seller.tranche_sats * 6)
        self.p.set_autopay(True)
        self.start_rental(auto=True)
        self.wallet.fail = "TIMEOUT"
        self.seller.say(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.assertIsNone(self.p.rental)
        self.assertEqual(self.p.spent24h(), self.seller.tranche_sats)   # counted, in case it went through
        self.assertTrue(self.p.state["payments"][0]["doubtful"])
        # now 6 more tranches do not fit in the day cap
        self.wallet.fail = None
        with self.assertRaises(p2hash.RentError) as e:
            self.p.rent(self.addr, 1, auto=True)
        self.assertIn("por día", str(e.exception))

    def test_budget_of_the_connection(self):
        self.wallet.budget_left_msat = self.seller.tranche_sats * 1000 * 2
        with self.assertRaises(p2hash.RentError) as e:
            self.p.rent(self.addr, 1)
        self.assertIn("da para 1 de 6", str(e.exception))

    def test_wallet_without_pay_offer_uses_pay(self):
        self.wallet.methods = ["get_info", "pay", "get_balance"]
        self.start_rental()
        self.seller.say(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.p.confirm(1)
        m, params = [c for c in self.wallet.calls if c[0] in ("pay", "pay_offer")][-1]
        self.assertEqual(m, "pay")
        self.assertEqual(params["payment"], OFFER)

    def test_timeouts(self):
        self.p.rent(self.addr, 1)
        self.clock.t += 121
        self.p.tick()
        self.assertIsNone(self.p.rental)
        self.assertIn("no contestó", self.p.state["history"][0]["reason"])
        # an unconfirmed tranche past the seller's grace: closed and the seller told
        self.setUp()
        self.start_rental()
        self.seller.say(type="due", rental_id="R1", n=1, sats=self.seller.tranche_sats, offer=OFFER, due_s=120)
        self.clock.t += 200
        self.p.tick()
        self.assertIsNone(self.p.rental)
        self.assertEqual(self.last("cancel")["rental_id"], "R1")

    def test_one_at_a_time_and_restart(self):
        self.start_rental()
        with self.assertRaises(p2hash.RentError):
            self.p.rent(self.addr, 1)
        p2 = p2hash.P2Hash(self.dir, lambda: self.conf, clock=self.clock)
        self.assertEqual(p2.pk, self.p.pk)                              # same buyer key
        self.assertEqual(p2.state["history"][0]["reason"], "panel reiniciado")
        self.assertEqual(p2.state["cancel_on_start"]["rental_id"], "R1")

    def test_view_never_shows_secrets(self):
        self.start_rental()
        v = json.dumps(self.p.view())
        self.assertNotIn(self.p.sk, v)
        self.assertNotIn(OFFER, v)
        self.assertNotIn("nostr+walletconnect", v)

    def test_settings_refusals(self):
        self.conf["p2hash"]["nwc"] = ""
        with self.assertRaises(p2hash.RentError):
            self.p.rent(self.addr, 1)
        self.conf["p2hash"]["nwc"] = "nostr+walletconnect://x"
        with self.assertRaises(p2hash.RentError):
            self.p.rent(self.addr, 30)                                   # beyond max_hours
        with self.assertRaises(p2hash.RentError):
            self.p.rent("30402:nobody:none", 1)
        with self.assertRaises(ValueError):
            self.p.set_wallet("lno1notawalletconnection", lambda u: None)


if __name__ == "__main__":
    unittest.main()
