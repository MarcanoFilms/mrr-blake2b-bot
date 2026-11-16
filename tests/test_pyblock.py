"""PyBLØCK rentals: parsing of recorded answers and the order flow against a local mock.

Run: python3 -m unittest discover -s tests
The mock implements the documented shapes of the PyBLØCK Market API v1; nothing
here talks to the real service. Fixtures hold no addresses, hosts or order ids
of ours (the order id below is made up).
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pyblock  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
FAKE_ID = "11111111-2222-4333-8444-555555555555"
FAKE_ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"      # BIP-173 test vector, not ours
FAKE_BOLT11 = "lnbc1fakeinvoicefortestsonly"
EXT_ID = "99999999-8888-4777-8666-555555555555"          # made up: an order created outside the panel
EXT_UNPAID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
EXT_MASK = FAKE_ADDR[:12] + "\u2026" + FAKE_ADDR[-6:]


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class ParseTests(unittest.TestCase):
    def test_packages(self):
        raw = load("pyblock_packages.json")
        cat = pyblock.parse_packages(raw, mrr_ref=2000)
        self.assertEqual(len(cat["packages"]), len(raw["packages"]))
        self.assertEqual(cat["promo"]["discount_pct"], 10)
        self.assertIn("xbtln", cat["pay_methods"])
        p = next(x for x in cat["packages"] if x["id"] == "p1x24")
        self.assertAlmostEqual(p["sat_th_day"], 2250.0)                 # 2250 sats / 1 TH / 1 day
        self.assertAlmostEqual(p["vs_mrr"], 0.125)
        two = next(s for s in p["steps"] if s["qty"] == 2)
        self.assertAlmostEqual(two["sat_th_day"], 4705 / 2)
        q = next(x for x in cat["packages"] if x["id"] == "p4_4x48")
        self.assertAlmostEqual(q["sat_th_day"], q["total_sats"] / 4.4 / 2)
        # available ones first, cheapest per TH/day first
        avail = [x["sat_th_day"] for x in cat["packages"] if x["available"]]
        self.assertEqual(avail, sorted(avail))

    def test_compare_recomputes(self):
        cat = pyblock.parse_packages(load("pyblock_packages.json"))
        self.assertIsNone(cat["packages"][0]["vs_mrr"])
        c2 = pyblock.compare(cat, 1000)
        self.assertIsNotNone(c2["packages"][0]["vs_mrr"])
        self.assertIsNone(cat["packages"][0]["vs_mrr"])                 # original untouched

    def test_order_created(self):
        o = pyblock.parse_order(load("pyblock_order_created.json"), now=1000)
        self.assertEqual(o["order_id"], FAKE_ID)
        self.assertEqual(o["status"], "pending_payment")
        self.assertEqual(o["invoice"], FAKE_BOLT11)
        self.assertEqual(o["expires_at"], 1600)

    def test_order_status_active(self):
        o = pyblock.parse_order(load("pyblock_order_active.json"), now=1000)
        self.assertEqual(o["status"], "active")
        self.assertAlmostEqual(o["live_ths"], 17.4)
        self.assertAlmostEqual(o["hashrate_ths"], 18)
        self.assertEqual(o["ends_at"], 1000 + 36000)

    def test_order_status_phs_and_xbt(self):
        o = pyblock.parse_order({"ok": True, "order": {"status": "pending_payment", "pay_method": "xbt",
                                 "xbt_address": "addr-x", "xbt_amount": "0.0012", "seconds_left": 60,
                                 "hashrate_phs": 0.0045, "live_hashrate_phs": 0}}, now=10)
        self.assertEqual(o["xbt_address"], "addr-x")
        self.assertEqual(o["expires_at"], 70)
        self.assertAlmostEqual(o["hashrate_ths"], 4.5)
        self.assertEqual(o["live_ths"], 0)

    def test_errors(self):
        self.assertEqual(pyblock._err_text({"ok": False, "errors": ["Order not found"]}), "Order not found")
        self.assertEqual(pyblock._err_text({"ok": False, "error": "invalid payout address"}), "invalid payout address")


class ExternalParseTests(unittest.TestCase):
    def test_extract_order_id(self):
        self.assertEqual(pyblock.extract_order_id("  " + EXT_ID + " "), EXT_ID)
        self.assertEqual(pyblock.extract_order_id("https://b.example.org:8443/rentals.php?order=" + EXT_ID + "&lang=es"), EXT_ID)
        self.assertEqual(pyblock.extract_order_id("https://b.example.org/api/market/v1.php/rentals/orders/" + EXT_ID), EXT_ID)
        self.assertIsNone(pyblock.extract_order_id(""))
        self.assertIsNone(pyblock.extract_order_id("abc"))
        self.assertIsNone(pyblock.extract_order_id("../../etc/passwd x"))

    def test_parse_external_fields(self):
        o = pyblock.parse_order({"ok": True, "order_id": EXT_ID, "status": "active", "hashrate_ths": 16.1,
                                 "hours": 48, "started_at": 1000, "created_at": 900, "remaining_secs": 50,
                                 "gateway": {"host": "h", "port": 1, "user": FAKE_ADDR + ".rig7"}}, now=2000)
        self.assertEqual(o["started_at"], 1000)
        self.assertEqual(o["created_at"], 900)
        self.assertEqual(o["gateway_user"], FAKE_ADDR + ".rig7")
        o = pyblock.parse_order({"ok": True, "order": {"id": EXT_ID, "btc_address": EXT_MASK, "duration_h": 24,
                                                       "hashrate_phs": 0.01, "kind": "order"}})
        self.assertEqual(o["payout_address"], EXT_MASK)
        self.assertEqual(o["hours"], 24)
        self.assertAlmostEqual(o["hashrate_ths"], 10)

    def test_address_matches(self):
        self.assertTrue(pyblock.address_matches(FAKE_ADDR, FAKE_ADDR))
        self.assertTrue(pyblock.address_matches(EXT_MASK, FAKE_ADDR))
        self.assertFalse(pyblock.address_matches(EXT_MASK, FAKE_ADDR[:-1] + "x"))
        self.assertFalse(pyblock.address_matches("bc1\u2026x", FAKE_ADDR))           # too little to go on
        self.assertFalse(pyblock.address_matches("", FAKE_ADDR))

    def test_unpaid(self):
        self.assertTrue(pyblock.is_unpaid({"status": "expired", "notified": {}}))
        self.assertFalse(pyblock.is_unpaid({"status": "expired", "notified": {"paid": 1}}))       # a paid rental that ran out
        self.assertFalse(pyblock.is_unpaid({"status": "expired", "started_at": 5}))
        self.assertFalse(pyblock.is_unpaid({"status": "expired", "percent_done": 100}))
        self.assertFalse(pyblock.is_unpaid({"status": "completed"}))
        p = pyblock.PyBlock(tempfile.mkdtemp(), lambda: {})
        pub = p.public({"ref": "r1", "status": "expired", "notified": {}, "order_id": "x" * 20})
        self.assertTrue(pub["unpaid"])
        self.assertEqual(pub["status_label"], pyblock.UNPAID_LABEL)
        self.assertFalse(pub["live"])
        pub = p.public({"ref": "r2", "status": "expired", "notified": {"paid": 1}})
        self.assertFalse(pub["unpaid"])
        self.assertEqual(pub["status_label"], "caducado")


class RenewTests(unittest.TestCase):
    def test_renew_price(self):
        cat = pyblock.parse_packages(load("pyblock_packages.json"))
        # 1 × 16.1 TH × 48 h: the only matching package, 59738 sats / 16.1 / 2 days
        self.assertAlmostEqual(pyblock.renew_price(cat, 16.1, 48, 1), 59738 / 16.1 / 2)
        # same unit and hours, two units: the step with qty 2
        self.assertAlmostEqual(pyblock.renew_price(cat, 1, 24, 2), 4705 / 2)
        # no step with that quantity: the cheapest step of the package
        self.assertAlmostEqual(pyblock.renew_price(cat, 16.1, 48, 3), 59738 / 16.1 / 2)
        self.assertIsNone(pyblock.renew_price(cat, 16.1, 24, 1))      # no such package now
        self.assertIsNone(pyblock.renew_price(None, 16.1, 48, 1))
        for p in cat["packages"]:
            p["available"] = False
        self.assertIsNone(pyblock.renew_price(cat, 16.1, 48, 1))       # sold out = nothing to renew at


class Mock(BaseHTTPRequestHandler):
    posts = []
    mode = "ok"            # ok | gateway | ratelimit | down
    status = "pending_payment"
    ext_status = "active"
    ext_mask = True

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.endswith("/rentals/packages"):
            return self._send(200, load("pyblock_packages.json"))
        if self.path.endswith("/rentals/orders/" + FAKE_ID):
            o = load("pyblock_order_active.json")
            o["order"]["status"] = Mock.status
            return self._send(200, o)
        if self.path.endswith("/rentals/orders/" + EXT_ID):
            return self._send(200, {"ok": True, "order_id": EXT_ID, "status": Mock.ext_status, "hashrate_ths": 16.1,
                                    "live_ths": 15.9, "hours": 48, "percent_done": 10.0, "created_at": 1000,
                                    "started_at": 1100, "remaining_secs": 150000, "total_sats": 57164,
                                    "pay_method": "xbt", "still_payable": False, "invoice": None,
                                    "tracking_url": "https://b.example.org/rentals.php?order=" + EXT_ID})
        if self.path.endswith("/rentals/orders/" + EXT_UNPAID):
            return self._send(200, {"ok": True, "order_id": EXT_UNPAID, "status": "expired", "hashrate_ths": 4.5,
                                    "hours": 24, "percent_done": 0, "started_at": None, "total_sats": 9000,
                                    "pay_method": "ln", "tracking_url": "https://b.example.org/rentals.php?order=" + EXT_UNPAID})
        if self.path.endswith("/rentals/orders/not-a-rental-0001"):
            return self._send(200, {"ok": True, "status": "paid", "kind": "payout"})
        if "/api/hashrate_status.php?id=" in self.path:
            if Mock.ext_mask:
                return self._send(200, {"ok": True, "order": {"id": EXT_ID, "btc_address": EXT_MASK, "kind": "order"}})
            return self._send(404, {"ok": False})
        if "/payout/gateway-test" in self.path:
            return self._send(200, {"ok": False, "error": "could not connect to host:port"})
        return self._send(404, {"ok": False, "errors": ["Order not found"]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Mock.posts.append(body)
        if Mock.mode == "gateway":
            return self._send(422, {"ok": False, "error": "Tu gateway no pasó el chequeo: could not connect"})
        if Mock.mode == "ratelimit":
            return self._send(429, {"ok": False, "error": "rate limited"})
        if Mock.mode == "down":
            return self._send(502, {"ok": False, "error": "bad gateway"})
        seen = sum(1 for p in Mock.posts if p.get("client_nonce") == body["client_nonce"]) > 1
        return self._send(200 if seen else 201, load("pyblock_order_created.json"))


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/api/market/v1.php"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Mock.posts, Mock.mode, Mock.status, Mock.ext_status, Mock.ext_mask = [], "ok", "pending_payment", "active", True
        self.dir = tempfile.mkdtemp()
        self.notices = []
        self.conf = {"rent_address": FAKE_ADDR, "fee_rate": 0.03,
                     "pyblock": {"api": self.base, "gateway": {"host": "gw.example.com", "port": 23334}}}
        self.p = pyblock.PyBlock(self.dir, lambda: self.conf, notify=lambda c, t: self.notices.append(t),
                                 gateway_clients=lambda: [{"worker": "x", "hashrate_th": 1.0}])

    def test_create_and_follow(self):
        o = self.p.create("p18x24", 2, "ln", "nonce-abcdef-0123456")
        self.assertEqual(o["status"], "pending_payment")
        self.assertEqual(o["payment"]["invoice"], FAKE_BOLT11)
        self.assertNotIn("order_id", o)
        sent = Mock.posts[0]
        self.assertNotIn("package", sent)   # PyBLØCK rejected ids with a decimal size
        self.assertEqual((sent["unit_th"], sent["hours"]), (18, 24))
        self.assertEqual(sent["qty"], 2)
        self.assertEqual(sent["gateway"]["host"], "gw.example.com")
        self.assertTrue(sent["gateway"]["user"].startswith(FAKE_ADDR + ".pyb"))
        self.assertNotIn("backup", sent)
        # the state file holds the secret and is private
        path = os.path.join(self.dir, "pyblock.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertIn(FAKE_ID, f.read())
        # the overview never carries it
        view = json.dumps(self.p.view())
        self.assertNotIn(FAKE_ID, view)
        self.assertNotIn("nonce-abcdef-0123456", view)
        # same nonce: no second POST
        self.p.create("p18x24", 2, "ln", "nonce-abcdef-0123456")
        self.assertEqual(len(Mock.posts), 1)
        # paid -> notice, then completed -> notice; no secrets in any notice
        Mock.status = "active"
        rec = self.p._find(ref=o["ref"])
        rec["checked"] = 0
        self.p.refresh(rec, force=True)
        self.assertEqual(rec["status"], "active")
        Mock.status = "completed"
        rec["checked"] = 0
        self.p.refresh(rec, force=True)
        self.assertEqual(len(self.notices), 3)
        for t in self.notices:
            self.assertNotIn(FAKE_ID, t)
            self.assertNotIn(FAKE_BOLT11, t)
        self.assertIn("pagado", self.notices[1])
        pub = self.p.public(rec)
        self.assertNotIn("payment", pub)                               # no invoice once it is over

    def test_view_prices_and_analysis(self):
        calls = []

        def analyse(worker, start, end, th):
            calls.append((worker, start, end, th))
            return {"delivered_th": 15.5, "pct": 15.5 / th * 100, "verdict": "ok"}
        self.p.analyse = analyse
        o = self.p.create("p18x24", 2, "ln", "nonce-view-00000001")
        rec = self.p._find(ref=o["ref"])
        v = self.p.view()["orders"][0]
        self.assertIsNone(v["analysis"])                               # not paid yet: nothing measured
        self.assertAlmostEqual(v["sat_th_day"], 65227 / 36)
        self.assertAlmostEqual(v["renew_sat_th_day"], 65227 / 36)      # same package, same qty
        rec.update(status="active", ends_at=200000.0)
        view = self.p.view()
        v = view["orders"][0]
        self.assertEqual(calls[-1], (rec["worker"], 200000.0 - 24 * 3600, 200000.0, 36))
        self.assertEqual(v["analysis"]["verdict"], "ok")
        self.assertAlmostEqual(view["totals"]["delivered_th"], 15.5)

    def test_gateway_refused(self):
        Mock.mode = "gateway"
        with self.assertRaises(pyblock.OrderError) as cm:
            self.p.create("p1x24", 1, "ln", "nonce-gateway-000001")
        self.assertFalse(cm.exception.retryable)
        self.assertIn("gateway", str(cm.exception))
        self.assertEqual(self.p.state["orders"], [])                   # nothing left behind

    def test_retry_same_nonce(self):
        Mock.mode = "ratelimit"
        with self.assertRaises(pyblock.OrderError) as cm:
            self.p.create("p1x24", 1, "xbtln", "nonce-retry-00000001")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.p.state["orders"]), 1)
        Mock.mode = "ok"
        o = self.p.create("p1x24", 1, "xbtln", "nonce-retry-00000001")
        self.assertEqual(o["status"], "pending_payment")
        self.assertEqual({p["client_nonce"] for p in Mock.posts}, {"nonce-retry-00000001"})
        self.assertEqual(len(self.p.state["orders"]), 1)

    def test_validation(self):
        with self.assertRaises(pyblock.OrderError):
            self.p.create("p1x24", 9, "ln", "nonce-validation-0001")   # max_qty 2
        with self.assertRaises(pyblock.OrderError):
            self.p.create("p1x24", 1, "card", "nonce-validation-0002")
        with self.assertRaises(pyblock.OrderError):
            self.p.create("nope", 1, "ln", "nonce-validation-0003")
        with self.assertRaises(pyblock.OrderError):
            self.p.create("p1x24", 1, "ln", "short")
        self.conf["pyblock"]["gateway"] = {"host": "", "port": 0}
        with self.assertRaises(pyblock.OrderError):
            self.p.create("p1x24", 1, "ln", "nonce-validation-0004")
        self.assertEqual(Mock.posts, [])

    def test_external_by_address(self):
        clients = [
            {"subscribed": True, "address": FAKE_ADDR, "worker": "kilo", "hashrate_th": 15.0, "label": "",
             "useragent": "pyblock-market/1.0"},
            {"subscribed": True, "address": FAKE_ADDR, "worker": "mrr123", "hashrate_th": 9.0, "label": "MRR proxy",
             "useragent": "pyblock-market/1.0"},                                # MRR's, not this order's
            {"subscribed": True, "address": FAKE_ADDR, "worker": "home", "hashrate_th": 1.0, "label": "LAN",
             "useragent": "cgminer"},                                           # own miner on the same address
            {"subscribed": True, "address": "bc1qother000000000000000000000000000", "worker": "kilo",
             "hashrate_th": 7.0, "label": "PyBLOCK rental", "useragent": "pyblock-market/1.0"},
        ]
        self.p.gateway_clients = lambda: clients
        calls = []
        self.p.analyse = lambda who, s, e, th: calls.append((who, s, e, th)) or {"delivered_th": 15.2, "pct": 94.4, "verdict": "ok"}
        o = self.p.add_external("https://b.example.org:8443/rentals.php?order=" + EXT_ID)
        self.assertTrue(o["external"])
        self.assertEqual(o["match"], "address")
        self.assertEqual(o["status"], "active")
        self.assertAlmostEqual(o["gateway_th"], 15.0)
        self.assertEqual(o["address_workers"], ["kilo"])
        rec = self.p._find(ref=o["ref"])
        self.assertEqual(rec["order_id"], EXT_ID)
        self.assertEqual(rec["address"], EXT_MASK)
        path = os.path.join(self.dir, "pyblock.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        view = self.p.view()
        dump = json.dumps(view)
        self.assertNotIn(EXT_ID, dump)                                  # neither the id nor its tracking link
        v = view["orders"][0]
        self.assertTrue(v["live"])
        self.assertEqual(calls[-1][0], ["kilo"])
        self.assertEqual(calls[-1][1], 1100)                             # PyBLØCK's started_at
        self.assertTrue(v["analysis"]["approx"])
        self.assertEqual(view["totals"]["active"], 1)
        # one notice, without the secret; paid before it got here: no "paid" notice later
        self.assertEqual(len(self.notices), 1)
        self.assertIn("fuera del panel", self.notices[0])
        self.assertNotIn(EXT_ID, self.notices[0])
        rec["checked"] = 0
        self.p.refresh(rec, force=True)
        self.assertEqual(len(self.notices), 1)
        # the same order twice: refused
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external(EXT_ID)
        # removable while it runs (only stops following it here)
        self.p.forget(o["ref"])
        self.assertEqual(self.p.state["orders"], [])
        with open(path) as f:
            self.assertNotIn(EXT_ID, f.read())

    def test_external_with_worker(self):
        self.p.gateway_clients = lambda: [{"subscribed": True, "address": FAKE_ADDR, "worker": "rig7", "hashrate_th": 16.0}]
        o = self.p.add_external(EXT_ID, FAKE_ADDR + ".rig7")
        self.assertEqual(o["match"], "worker")
        self.assertEqual(o["worker"], "rig7")
        self.assertEqual(o["address"], FAKE_ADDR)
        self.assertAlmostEqual(o["gateway_th"], 16.0)
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external(EXT_ID, "bad worker!")

    def test_external_without_address(self):
        Mock.ext_mask = False
        o = self.p.add_external(EXT_ID)
        self.assertEqual(o["match"], "none")
        self.assertIsNone(o["gateway_th"])
        self.assertIsNone(self.p.view()["orders"][0]["analysis"])

    def test_external_unpaid_and_refusals(self):
        o = self.p.add_external(EXT_UNPAID)
        self.assertTrue(o["unpaid"])
        self.assertEqual(o["status_label"], pyblock.UNPAID_LABEL)
        self.assertFalse(o["live"])
        self.assertNotIn("payment", o)
        with self.assertRaises(pyblock.OrderError) as cm:
            self.p.add_external("ffffffff-0000-4000-8000-000000000000")     # unknown to PyBLØCK
        self.assertIn("no conoce", str(cm.exception))
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external("not-a-rental-0001")
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external("x")
        self.assertEqual(len(self.p.state["orders"]), 1)
        self.p.forget(o["ref"])

    def test_gateway_test_error_text(self):
        r = self.p.gateway_test(force=True)
        self.assertFalse(r["ok"])
        self.assertIn("could not connect", r["error"])


class AuditWorkersTests(unittest.TestCase):
    def test_several_workers_measured_together(self):
        import audit
        a = audit.Audit(tempfile.mkdtemp(), {"audit": {"db": "a.db"}})
        for ts, sh in ((1000, 0), (1010, 10), (1020, 20)):
            a.record([{"subscribed": True, "skey": "k1", "subbed_s": ts - 990, "worker": "w1", "shares_accepted": sh, "vdiff": "1000"},
                      {"subscribed": True, "skey": "k2", "subbed_s": ts - 990, "worker": "w2", "shares_accepted": sh * 2, "vdiff": "1000"},
                      {"subscribed": True, "skey": "k3", "subbed_s": ts - 990, "worker": "w3", "shares_accepted": 99, "vdiff": "1000"}], 10)
            with a.db() as c:   # record() stamps with the clock: put the rows at ts
                c.execute("UPDATE samples SET ts=?, session_start=990 WHERE ts > 100000", (ts,))
        one = a._work("w1", 995, 1020)
        both = a._work(["w1", "w2"], 995, 1020)
        self.assertEqual(one[1], 20)
        self.assertEqual(both[1], 60)
        self.assertEqual(both[2], 2)


if __name__ == "__main__":
    unittest.main()
