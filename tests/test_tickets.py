"""Auto-ticket, manual history deletion and rentals from other sources.

Run: python3 -m unittest discover -s tests
Nothing here talks to MRR or Telegram: the API and the notifier are stubs.
"""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import audit as audit_mod  # noqa: E402
import dashboard  # noqa: E402
import tickets  # noqa: E402

NOW = 1_800_000_000


def rental(**kw):
    r = {"id": 5900001, "rig_id": 42, "rig_name": "Big rig", "advertised_th": 10.0, "average_th": 9.8,
         "average_pct": "98", "start_unix": NOW - 10 * 3600, "end_unix": NOW + 14 * 3600, "length_h": 24,
         "left_h": 14, "paid": "0.00100000", "currency": "BTC", "ended": False, "rig_online": True}
    r.update(kw)
    return r


def analysis(**kw):
    a = {"worker": "mrr5900001", "shared": False, "verdict": "short", "delivered_th": 6.0, "pct": 60.0,
         "shares": 600, "expected_shares": 1000.0, "z": 12.6, "elapsed_h": 10.0, "observed_from": NOW - 10 * 3600,
         "connected": True}
    a.update(kw)
    return a


class FakeAudit:
    """audit.Audit backed by a temporary audit.db, with a fixed analysis for the mid-rental view."""
    def __init__(self, a):
        self.tmp = tempfile.mkdtemp()
        self.real = audit_mod.Audit(self.tmp, {"audit": {"db": "audit.db"}})
        self.a = a

    def __getattr__(self, k):
        return getattr(self.real, k)

    def view(self, rentals):
        return {"rentals": [{"id": r["id"], "analysis": self.a} for r in rentals]}


def hist(**kw):
    """A finished MRR rental in the history: 10 TH/s paid, 9 400 shares of 10 000 (6 sigma short)."""
    h = {"rental_id": "5900001", "rig_id": "42", "rig_name": "Big rig", "start_unix": NOW - 33 * 3600,
         "end_unix": NOW - 9 * 3600, "advertised_th": 10.0, "mrr_average_th": 9.8, "delivered_th": 9.4, "pct": 94.0,
         "verdict": "short", "shares": 9400, "expected_shares": 10000.0, "paid": "0.00100000", "currency": "BTC",
         "address": None, "worker": "mrr5900001", "finished": NOW - 9 * 3600 + 120, "source": "mrr"}
    h.update(kw)
    return h


class FakeAPI:
    def __init__(self, fail=False, was_refunded=False, refund_tx=False):
        self.calls = []
        self.fail, self.was_refunded, self.refund_tx = fail, was_refunded, refund_tx

    def call(self, method, endpoint, params=None, body=None):
        self.calls.append((method, endpoint, params, body))
        if method == "GET" and endpoint.startswith("/rental/"):
            return {"id": "5900001", "was_refunded": self.was_refunded, "ended": True}
        if endpoint == "/account/transactions":
            tx = [{"id": "1", "type": "Credit/Refund", "amount": "0.00006000", "rental": params["rental"]}] if self.refund_tx else []
            return {"total": str(len(tx)), "transactions": tx}
        if self.fail:
            raise RuntimeError("HTTP 500")
        return [{"id": "5900001", "success": True}]

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]


S = tickets.settings({})


class FinalTests(unittest.TestCase):
    def ok(self, **kw):
        return tickets.final_eligible(hist(**kw), S, NOW)[0]

    def test_short_beyond_noise(self):
        self.assertTrue(self.ok())

    def test_guards(self):
        self.assertFalse(self.ok(end_unix=NOW - 3 * 3600))              # MRR's own review may still refund it
        self.assertFalse(self.ok(end_unix=NOW - 13 * 3600))             # 12-hour window over
        self.assertFalse(self.ok(source="pyblock"))
        self.assertFalse(self.ok(delivered_th=None))
        self.assertFalse(self.ok(expected_shares=100, shares=80, pct=80))   # too few shares to judge

    def test_noise(self):
        # 99.5 % of 10 000 expected shares is 0.5 sigma short: within noise, not flagged
        self.assertFalse(self.ok(shares=9950, pct=99.5, delivered_th=9.95))
        # 97 % of 10 000 is 3 sigma exactly: not beyond it
        self.assertFalse(self.ok(shares=9700, pct=97.0))
        # 96.9 % is beyond 3 sigma: flagged although it is above 95 %
        self.assertTrue(self.ok(shares=9690, pct=96.9))

    def test_max_pct(self):
        s = dict(S, max_pct=95.0)
        self.assertTrue(tickets.final_eligible(hist(pct=94.0), s, NOW)[0])
        self.assertFalse(tickets.final_eligible(hist(shares=9600, pct=96.0), s, NOW)[0])


class EarlyTests(unittest.TestCase):
    def ok(self, r=None, a=None):
        return tickets.early_eligible(r or rental(), a if a is not None else analysis(), S, NOW)[0]

    def test_sad_mid_rental(self):
        self.assertTrue(self.ok())
        self.assertTrue(self.ok(a=analysis(verdict="nothing", delivered_th=0.0, shares=0, connected=False)))

    def test_guards(self):
        self.assertFalse(self.ok(rental(ended=True)))
        self.assertFalse(self.ok(rental(rig_online=None)))
        self.assertFalse(self.ok(a=analysis(verdict="ok")))
        self.assertFalse(self.ok(a=analysis(delivered_th=9.6)))
        self.assertFalse(self.ok(rental(start_unix=NOW - 3600, end_unix=NOW + 23 * 3600)))
        self.assertFalse(self.ok(a=analysis(expected_shares=50)))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.au = FakeAudit(analysis())
        self.au.add_history(hist())
        self.sent = []
        self.notify = lambda c, text: self.sent.append(text)

    def run_check(self, api, cfg=None, rentals=None):
        return tickets.check(self.au, rentals if rentals is not None else [rental()], lambda: api, self.notify,
                             cfg or {}, log=lambda m: None, now=NOW)

    def test_off_by_default(self):
        api = FakeAPI()
        self.assertEqual(self.run_check(api), [])
        self.assertEqual((self.sent, api.calls), ([], []))
        self.assertEqual(tickets.switches(self.au), {"enabled": False, "message_owner": False, "warn_early": False})

    def test_after_the_end_once(self):
        tickets.set_switches(self.au, enabled=True)
        api = FakeAPI()
        self.assertEqual(self.run_check(api), ["5900001"])
        self.assertEqual(self.run_check(api), [])                    # never twice
        self.assertEqual(api.puts(), [])                             # no message: switch off
        self.assertEqual(len(self.sent), 1)
        self.assertIn("terminó entregando menos", self.sent[0])
        self.assertIn("Te quedan 3 h 00 min", self.sent[0])
        self.assertIn("6.0 σ", self.sent[0])
        self.assertIn("it finished below the advertised hashrate", self.sent[0])
        self.assertIn("MRR's own average: 9.80 TH/s (98 %)", self.au.tickets()[0]["evidence"])

    def test_refunded_flag_skips(self):
        tickets.set_switches(self.au, enabled=True)
        self.assertEqual(self.run_check(FakeAPI(was_refunded=True)), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.au.tickets()[0]["verdict"], "refunded")

    def test_refund_transaction_skips(self):
        tickets.set_switches(self.au, enabled=True)
        api = FakeAPI(refund_tx=True)
        self.assertEqual(self.run_check(api), [])
        self.assertEqual(self.sent, [])
        self.assertIn(("GET", "/account/transactions", {"rental": 5900001, "type": "credit/refund", "limit": 20}, None),
                      api.calls)

    def test_message_owner_after_end(self):
        tickets.set_switches(self.au, enabled=True, message_owner=True)
        api = FakeAPI()
        self.run_check(api)
        (m, ep, _, body), = api.puts()
        self.assertEqual((m, ep), ("PUT", "/rental/5900001/message"))
        self.assertIn("standard deviations below", body["message"])
        self.assertEqual(self.au.tickets()[0]["message_sent"], 1)

    def test_message_failure_still_notifies(self):
        tickets.set_switches(self.au, enabled=True, message_owner=True)
        self.run_check(FakeAPI(fail=True))
        t = self.au.tickets()[0]
        self.assertEqual(t["message_sent"], 0)
        self.assertIn("HTTP 500", t["error"])
        self.assertIn("No se pudo escribir", self.sent[0])

    def test_early_warning_only_when_on(self):
        tickets.set_switches(self.au, warn_early=True)              # the after-end notice stays off
        api = FakeAPI()
        self.assertEqual(self.run_check(api), ["5900001:early"])
        self.assertEqual(self.run_check(api), [])
        (m, ep, _, body), = api.puts()
        self.assertEqual(ep, "/rental/5900001/message")
        self.assertTrue(body["message"].startswith("Hello, a heads-up from the renter"))
        self.assertIn("va corto", self.sent[0])

    def test_read_only_does_nothing(self):
        tickets.set_switches(self.au, enabled=True, message_owner=True, warn_early=True)
        api = FakeAPI()
        self.assertEqual(self.run_check(api, {"read_only": True}), [])
        self.assertEqual((self.sent, api.calls), ([], []))


class HistoryTests(unittest.TestCase):
    def test_add_and_delete(self):
        au = audit_mod.Audit(tempfile.mkdtemp(), {"audit": {"db": "audit.db"}})
        au.add_history({"rental_id": "pyb-abc", "rig_name": "PyBLØCK 1× 10T", "start_unix": NOW - 3600,
                        "end_unix": NOW, "advertised_th": 10.0, "verdict": "ok", "paid": "5000",
                        "currency": "sats", "finished": int(time.time()), "source": "pyblock"})
        h = au.history()["history"]
        self.assertEqual((h[0]["rental_id"], h[0]["source"]), ("pyb-abc", "pyblock"))
        self.assertEqual(au.delete_history("nope"), 0)
        self.assertEqual(au.delete_history("pyb-abc"), 1)
        self.assertEqual(au.history()["history"], [])

    def test_old_db_gets_the_source_column(self):
        tmp = tempfile.mkdtemp()
        import sqlite3
        c = sqlite3.connect(os.path.join(tmp, "audit.db"))
        c.execute("""CREATE TABLE history (rental_id TEXT PRIMARY KEY, rig_id TEXT, rig_name TEXT, start_unix INTEGER,
            end_unix INTEGER, advertised_th REAL, mrr_average_th REAL, delivered_th REAL, pct REAL, verdict TEXT,
            shares INTEGER, expected_shares REAL, paid TEXT, currency TEXT, address TEXT, worker TEXT,
            finished INTEGER NOT NULL)""")
        c.execute("INSERT INTO history (rental_id, finished) VALUES ('1', ?)", (int(time.time()),))
        c.commit()
        c.close()
        au = audit_mod.Audit(tmp, {"audit": {"db": "audit.db"}})
        self.assertIsNone(au.history()["history"][0]["source"])     # old rows read as MRR


class TrackTests(unittest.TestCase):
    """A rental MRR's list leaves out once must not land in the history while it still runs."""
    def mrr_rental(self, rid, end):
        return {"id": rid, "rig": {"id": 7, "name": "r"}, "hashrate": {"advertised": {"hash": "10", "type": "th"}},
                "start": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(end - 86400)),
                "end": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(end)), "ended": False}

    def test_missing_once_is_not_finished(self):
        au = audit_mod.Audit(tempfile.mkdtemp(), {"audit": {"db": "audit.db"}})
        now = int(time.time())
        r = self.mrr_rental("77", now + 3600 * 5)

        class API:
            def call(self, m, ep, params=None, body=None):
                return dict(r, price={"paid": "0.1", "currency": "BTC"})
        au.track(API(), [r], now)
        au.track(API(), [], now + 120)                 # left out of the list once
        self.assertEqual(au.history()["history"], [])
        au.add_history({"rental_id": "77", "finished": now, "verdict": "ok"})   # an old wrong entry
        au.track(API(), [r], now + 240)                # running again: the wrong entry goes
        self.assertEqual(au.history()["history"], [])


class OwnMinerTests(unittest.TestCase):
    def test_own_worker_is_not_a_rental(self):
        e = {"user": "bc1qexample.kilombino", "worker": "kilombino", "useragent": "pyblock-market/1.0"}
        self.assertTrue(dashboard.is_own_miner(e, ["bc1qexample.kilombino"], []))
        self.assertTrue(dashboard.is_own_miner(e, ["kilombino"], []))
        self.assertFalse(dashboard.is_own_miner(e, ["bc1qother.kilombino"], []))
        self.assertTrue(dashboard.is_own_miner(e, [], ["PyBLOCK-market"]))
        self.assertFalse(dashboard.is_own_miner(e, [], []))

    def test_parse_marks_own(self):
        row = ("<TR><TD>1/2</TD><TD>203.0.113.9</TD><TD>bc1qexample.kilombino</TD><TD>abcdef01 12.0s</TD><TD>1s</TD>"
               "<TD>4096</TD><TD>1.0T (10)</TD><TD>0 (0) 0.00%</TD><TD>5.00 Th/s (3.0s)</TD><TD></TD><TD>pyblock-market/1.0</TD></TR>")
        dashboard.rdns_cache["203.0.113.9"] = ""
        c, = dashboard.parse_clients(row, [], [], ["bc1qexample.kilombino"], [])
        self.assertEqual((c["label"], c["own"], c["own_miner"]), ("tu minero", True, True))


class SourceTests(unittest.TestCase):
    SRC = dashboard.DASH_DEFAULTS["other_sources"]

    def test_classify(self):
        self.assertEqual(dashboard.classify_source({"useragent": "P2PHash-Agent/0.3"}, self.SRC), "p2phash rental")
        self.assertEqual(dashboard.classify_source({"host": "x.tidehash.com"}, self.SRC), "TideHash rental")
        self.assertEqual(dashboard.classify_source({"worker": "rig1", "useragent": "cgminer"}, self.SRC), "")

    def test_parse_clients_labels(self):
        row = ("<TR><TD>1/2</TD><TD>203.0.113.9</TD><TD>bc1qexample.{w}</TD><TD>abcdef01 12.0s</TD><TD>1s</TD>"
               "<TD>4096</TD><TD>1.0T (10)</TD><TD>0 (0) 0.00%</TD><TD>5.00 Th/s (3.0s)</TD><TD></TD><TD>{ua}</TD></TR>")
        html = row.format(w="p2p", ua="p2phash-agent/1.0") + row.format(w="kilo", ua="pyblock-market/1.0")
        dashboard.rdns_cache["203.0.113.9"] = ""
        cl = dashboard.parse_clients(html, [], self.SRC)
        self.assertEqual([c["label"] for c in cl], ["p2phash rental", "PyBLOCK rental"])


if __name__ == "__main__":
    unittest.main()
