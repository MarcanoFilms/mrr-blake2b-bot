"""PyBLØCK MARKET rentals made outside the panel, and detected connections without a worker.

Run: python3 -m unittest discover -s tests
The mock answers with the shapes of the Market API v1 (GET /orders/{secret},
GET /rigs) as their own order page reads them. Secrets, ids and addresses are
made up.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import audit  # noqa: E402
import pyblock  # noqa: E402

SECRET = "0123456789abcdef0123456789abcdef"
MID = "25f278f0aaaabbbb"
FAKE_ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"      # BIP-173 test vector, not ours


class Mock(BaseHTTPRequestHandler):
    status = "activo"
    now = 1_000_000
    rig_state = "rented"

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
        if self.path.endswith("/orders/" + SECRET):
            return self._send(200, {
                "ok": True, "now": Mock.now, "order": "a1b2c3", "status": Mock.status, "created": Mock.now - 4000,
                "dest": "gw.example.com:23334", "user": FAKE_ADDR + ".yuge", "total_sats": 565320, "hours": 3,
                "start": Mock.now - 3600, "end": Mock.now + 7200, "paused_s": 0,
                "rigs": [{"rig": "kilombino", "miner": "m·25f278f0", "ths": 13.8, "ths_avg": 13.5,
                          "delivered_th": 13.5 * 3600, "rej_pct": 0.1, "status": "activo", "refund_sats": None}],
                "refund": ({"status": "pagado", "sats": 1200} if Mock.status == "liquidado" else None)})
        if self.path.endswith("/rigs"):
            return self._send(200, {"ok": True, "now": Mock.now, "rigs": [
                {"miner": "m·25f278f0", "mid": MID, "rig": "kilombino", "ths": 12.5, "price": 990110,
                 "state": Mock.rig_state, "free_at": Mock.now + 7200}]})
        return self._send(404, {"ok": False, "error": "No existe esa orden."})


class MarketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/api/market/v1.php"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Mock.status, Mock.rig_state = "activo", "rented"
        self.dir = tempfile.mkdtemp()
        self.notices = []
        self.conf = {"rent_address": FAKE_ADDR, "pyblock": {"api": self.base}}
        self.p = pyblock.PyBlock(self.dir, lambda: self.conf, notify=lambda c, t: self.notices.append(t),
                                 gateway_clients=lambda: [])

    def test_parse_refs(self):
        self.assertEqual(pyblock.parse_market_ref(SECRET), ("secret", SECRET))
        self.assertEqual(pyblock.parse_market_ref(SECRET.upper()), ("secret", SECRET))
        self.assertEqual(pyblock.parse_market_ref(f"https://b.example.org:8443/market_minero.php?o={SECRET}&lang=es"),
                         ("secret", SECRET))
        for t in ("m·25f278f0", "m-25f278f0", "M.25F278F0", "m 25f278f0"):
            self.assertEqual(pyblock.parse_market_ref(t), ("mid", "25f278f0"), t)
        self.assertEqual(pyblock.parse_market_ref(MID), ("mid", MID))
        self.assertEqual(pyblock.parse_market_ref(f"https://b.example.org/market_minero.php?m={MID}"), ("mid", MID))
        self.assertEqual(pyblock.parse_market_ref("https://b.example.org/rentals.php?order=abc-123-def"), (None, None))
        self.assertEqual(pyblock.parse_market_ref("11111111-2222-4333-8444-555555555555"), (None, None))

    def test_order_by_secret(self):
        o = self.p.add_external(f"https://b.example.org/market_minero.php?o={SECRET}")
        self.assertTrue(o["market"])
        self.assertEqual(o["status"], "active")
        self.assertEqual(o["worker"], "yuge")                  # from the order's stratum user
        self.assertEqual(o["match"], "worker")
        self.assertAlmostEqual(o["th"], 13.8)
        self.assertEqual(o["total_sats"], 565320)
        self.assertEqual(o["name"], "market #a1b2c3 · kilombino (m·25f278f0)")
        self.assertFalse(o["estimated"])
        rec = self.p._find(ref=o["ref"])
        self.assertIn("market_minero.php?o=" + SECRET, rec["tracking_url"])
        self.assertAlmostEqual(rec["ends_at"] - rec["started_at"], 3 * 3600, delta=2)
        v = json.dumps(self.p.view())
        self.assertNotIn(SECRET, v)
        self.assertNotIn(SECRET, " ".join(self.notices))
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external(SECRET)                         # twice
        # it ends: liquidated with a refund
        Mock.status = "liquidado"
        finished = []
        self.p.on_finish = finished.append
        rec["checked"] = 0
        self.p.refresh(rec, force=True)
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["refund_sats"], 1200)
        self.assertEqual(len(finished), 1)

    def test_unknown_secret(self):
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external("f" * 32)

    def test_follow_by_miner_id(self):
        o = self.p.add_external("m·25f278f0")
        self.assertTrue(o["market"] and o["estimated"])
        self.assertEqual(o["match"], "address")
        self.assertEqual(o["status"], "active")
        rec = self.p._find(ref=o["ref"])
        self.assertAlmostEqual(rec["hours"], 2.0, delta=0.05)
        self.assertGreater(rec["total_sats"], 0)
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external("m-25f278f0")                   # already followed
        Mock.rig_state = "available"
        rec["checked"] = 0
        self.p.refresh(rec, force=True)
        self.assertEqual(rec["status"], "completed")
        # a rig that is not rented cannot be added by its id alone
        with self.assertRaises(pyblock.OrderError):
            self.p.add_external("m·25f278f0")

    def test_address_match_without_worker(self):
        """A market connection with only the address as stratum user is measured by its key."""
        o = self.p.add_external("m·25f278f0")
        clients = [{"subscribed": True, "address": FAKE_ADDR, "worker": "", "hashrate_th": 12.0,
                    "label": "PyBLOCK rental", "useragent": "Yuge"}]
        self.p.gateway_clients = lambda: clients
        calls = []
        self.p.analyse = lambda who, s, e, th: calls.append(who) or {"delivered_th": 12.0, "pct": 96.0, "verdict": "ok"}
        v = self.p.view()["orders"][0]
        self.assertEqual(v["address_workers"], ["(sin worker)"])
        self.assertEqual(calls[-1], ["@pyblock:" + FAKE_ADDR])
        self.assertTrue(v["analysis"]["approx"])
        self.assertEqual(o["ref"], v["ref"])


class SampleKeyTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(audit.sample_key({"worker": "mrr1", "address": "a", "label": "MRR proxy"}), "mrr1")
        self.assertEqual(audit.sample_key({"worker": "", "address": "bc1qx", "label": "PyBLOCK rental"}), "@pyblock:bc1qx")
        self.assertEqual(audit.sample_key({"worker": "", "address": "bc1qx", "label": "TideHash rental"}), "@tidehash:bc1qx")
        self.assertIsNone(audit.sample_key({"worker": "", "address": "bc1qx", "label": "LAN"}))     # own miners: no key
        self.assertIsNone(audit.sample_key({"worker": "", "address": "", "label": "PyBLOCK rental"}))
        k = {"worker": "", "address": "bc1qx", "label": "PyBLOCK rental"}
        self.assertEqual(audit.sample_key(k), pyblock.PyBlock.conn_key(k))   # both sides agree

    def test_measured_by_address(self):
        d = tempfile.mkdtemp()
        a = audit.Audit(d, {"audit": {"db": os.path.join(d, "a.db")}})
        cl = {"subscribed": True, "skey": "1_1_1_1", "subbed_s": 5, "worker": "", "address": "bc1qx",
              "label": "PyBLOCK rental", "shares_accepted": 0, "vdiff": "65536"}
        import time
        t0 = time.time()
        a.record([cl], 10)
        a.record([dict(cl, shares_accepted=40)], 10)
        work, shares, conns, _, last = a._work("@pyblock:bc1qx", t0 - 20, time.time() + 1)
        self.assertEqual(shares, 40)
        self.assertEqual(work, 40 * 65536)


if __name__ == "__main__":
    unittest.main()
