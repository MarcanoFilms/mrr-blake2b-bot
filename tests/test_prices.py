"""Prices shown on each MRR rental: what it was rented at and what extending it costs now.

Run: python3 -m unittest discover -s tests
Nothing here talks to MRR: the API object is a stub.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dashboard  # noqa: E402


def rig(rid, price, unit="th"):
    return {"id": rid, "price": {"type": unit, "BTC": {"price": price, "hour": "0", "enabled": True}}}


class PriceTests(unittest.TestCase):
    def test_paid(self):
        # MRR's `paid` leaves out its 3 % fee (it is charged apart): 0.00004539 BTC, 3 TH/s, 24 h
        self.assertAlmostEqual(dashboard.paid_sat_th_day("0.00004539", 3.0, 24, 0.03), 4539 * 1.03 / 3)
        self.assertAlmostEqual(dashboard.paid_sat_th_day("0.00326700", 19.0, 240, 0), 326700 / 19 / 10)
        for bad in ((None, 3, 24), ("0", 3, 24), ("x", 3, 24), ("0.1", 0, 24), ("0.1", 3, 0)):
            self.assertIsNone(dashboard.paid_sat_th_day(*bad, 0.03))

    def test_rig_price_units(self):
        # the same as MRR's extend quote: 0.00001696 BTC/TH/day x 3 TH x 1 day x 1.03 = 0.00005241
        v = dashboard.rig_price_sat_th_day(rig(1, "0.00001696"), "BTC", 0.03)
        self.assertAlmostEqual(v, 1696 * 1.03)
        self.assertAlmostEqual(round(v * 3), 5241)
        # priced per GH/day: x1000 per TH; per PH/day: /1000
        self.assertAlmostEqual(dashboard.rig_price_sat_th_day(rig(1, "0.00000002", "gh"), "BTC", 0), 2000)
        self.assertAlmostEqual(dashboard.rig_price_sat_th_day(rig(1, "0.002", "ph"), "BTC", 0), 200)
        self.assertIsNone(dashboard.rig_price_sat_th_day(rig(1, "0"), "BTC", 0.03))
        self.assertIsNone(dashboard.rig_price_sat_th_day(rig(1, "0.1", "sol"), "BTC", 0.03))
        self.assertIsNone(dashboard.rig_price_sat_th_day({"price": {}}, "BTC", 0.03))

    def test_rig_prices_batched_and_cached(self):
        calls = []

        class Api:
            state = {}

            def call(self, method, endpoint, params=None, body=None):
                calls.append(endpoint)
                ids = endpoint.rsplit("/", 1)[1].split(";")
                return [rig(int(i), "0.00002") for i in ids] if len(ids) > 1 else rig(int(ids[0]), "0.00002")

        old = dashboard.mrr_api, dashboard.mrr_bot.save_state
        dashboard.mrr_api, dashboard.mrr_bot.save_state = (lambda c: Api()), (lambda s: None)
        dashboard._rig_prices.update(at=0, ids=frozenset(), data={})
        try:
            c = {"currency": "BTC", "fee_rate": 0.03}
            p = dashboard.rig_prices(["2", "1", "2"], c)
            self.assertEqual(calls, ["/rig/1;2"])                      # one call for every rig
            self.assertAlmostEqual(p["1"], 2000 * 1.03)
            dashboard.rig_prices(["1"], c)                              # a subset: from the cache
            self.assertEqual(len(calls), 1)
            dashboard.rig_prices(["3"], c)                              # a new rig: asked again
            self.assertEqual(calls[-1], "/rig/3")
            self.assertEqual(dashboard.rig_prices([], c), {})
        finally:
            dashboard.mrr_api, dashboard.mrr_bot.save_state = old


if __name__ == "__main__":
    unittest.main()
