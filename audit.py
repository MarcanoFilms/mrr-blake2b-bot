"""Rental audit: does the hashrate you pay for on MRR actually reach your gateway?

The dashboard already polls the DATUM gateway's client list every few seconds.
This module keeps those samples in a small SQLite file and, for every active
MRR rental, compares three numbers:

  * what you paid for (advertised TH/s),
  * what MRR says the rig delivered (its own average),
  * what your gateway actually accepted from that rental's worker.

How the work is counted
-----------------------
Each stratum connection has its own cumulative "shares accepted" counter.
Connections come and go (MRR's proxies reconnect every few tens of seconds),
so the work of a rental is the sum of the *increments* of every connection
that carried its worker: a connection born inside the window counts from
zero, one that already existed counts only from its first sample.

Work is counted as accepted shares x the share difficulty (vardiff) and not
from the cumulative difficulty column, because recent gateways print that
column with an SI prefix and three significant digits ("2.10E"); on a
connection that lives for days it does not move for hours.

The verdict
-----------
Share counting is a Poisson process. The test is made against the number of
shares the *contracted* hashrate should have produced in the window
(expected = TH/s x 1e12 x seconds / (vardiff x 2^32), sigma = sqrt(expected)),
never against the error of what was measured: with one share the measured
error is 100 % and would swallow any shortfall. A rental is "short" only when
it is more than 3 sigmas below the expectation.

One worker per rental
---------------------
Rented rigs all arrive through MRR's proxies, from a handful of IPs and with
changing user agents, so neither IP nor user agent tells two rentals apart.
The worker name (the part of the stratum user after the first dot) does. If
`audit.assign_workers` is on, every active rental that shares its worker with
another one gets its pool user rewritten through the MRR API to
`<address>.<worker_prefix><rental id>`. That call changes the pool of a rental
that is already paid for; it never rents or extends anything. Work recorded
before a rental got its own worker cannot be separated, so it is judged only
from that moment on.
"""
import contextlib
import math
import os
import sqlite3
import threading
import time

HASHES_PER_DIFF = 2 ** 32
SI = {"": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "Z": 1e21}

DEFAULTS = {
    "enabled": True,
    "db": "audit.db",            # relative to the repo directory
    "retention_days": 7,
    "assign_workers": False,     # rewrite each rental's pool user to a worker of its own
    "worker_prefix": "mrr",
    "sync_seconds": 120,
    "sigma": 3.0,
    "offline_after_s": 120,
    "series_hours": 6,
    "series_bucket_s": 600,
    "history_days": 7,           # finished rentals kept in the history
    "favorite_pct": 95,          # a finished rental at or above this % of what was paid...
                                 # ...with an ok/over verdict makes its rig a favourite
}

_lock = threading.Lock()
_status = {"ok": None, "at": 0, "error": None, "renamed": []}


def settings(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("audit") or {})
    return out


def parse_diff(txt):
    """Share difficulty from a gateway cell: '4096' (plain) or '17.6T' (work, SI prefix)."""
    import re
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKMGTPEZ]?)", str(txt or ""))
    if not m:
        return None
    v = float(m.group(1))
    return v * SI[m.group(2)] / HASHES_PER_DIFF if m.group(2) else v


class Audit:
    def __init__(self, here, cfg):
        self.s = settings(cfg)
        path = self.s["db"]
        self.path = path if os.path.isabs(path) else os.path.join(here, path)
        self.poll = 10
        self._last_prune = 0
        self._init()

    # ------------------------------------------------------------------ storage
    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()   # `with conn` alone commits but does not close

    def _init(self):
        with self.db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER NOT NULL, skey TEXT NOT NULL, session_start INTEGER NOT NULL,
                worker TEXT, shares INTEGER NOT NULL, vdiff REAL)""")
            c.execute("CREATE INDEX IF NOT EXISTS i_samples_ts ON samples(ts)")
            c.execute("CREATE INDEX IF NOT EXISTS i_samples_worker ON samples(worker, ts)")
            c.execute("""CREATE TABLE IF NOT EXISTS rental_worker (
                rental_id TEXT PRIMARY KEY, worker TEXT, observe_from INTEGER, updated INTEGER)""")
            c.execute("""CREATE TABLE IF NOT EXISTS active (
                rental_id TEXT PRIMARY KEY, data TEXT NOT NULL, seen INTEGER NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS history (
                rental_id TEXT PRIMARY KEY, rig_id TEXT, rig_name TEXT, start_unix INTEGER, end_unix INTEGER,
                advertised_th REAL, mrr_average_th REAL, delivered_th REAL, pct REAL, verdict TEXT,
                shares INTEGER, expected_shares REAL, paid TEXT, currency TEXT, address TEXT, worker TEXT,
                finished INTEGER NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS favorites (
                rig_id TEXT PRIMARY KEY, rig_name TEXT, rentals INTEGER NOT NULL, avg_pct REAL, last_pct REAL,
                last_at INTEGER)""")
            cols = [r[1] for r in c.execute("PRAGMA table_info(rental_worker)")]
            if "address" not in cols:   # the payout address of the rental's pool user
                c.execute("ALTER TABLE rental_worker ADD COLUMN address TEXT")

    def record(self, clients, poll_seconds):
        """Store one gateway poll (the parsed client list of dashboard.py)."""
        self.poll = poll_seconds
        now = int(time.time())
        rows = []
        for c in clients:
            if not c.get("subscribed") or not c.get("skey"):
                continue
            rows.append((now, c["skey"], int(now - (c.get("subbed_s") or 0)), c.get("worker") or None,
                         int(c.get("shares_accepted") or 0), parse_diff(c.get("vdiff"))))
        with self.db() as conn:
            conn.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?)", rows)
            if now - self._last_prune > 3600:
                conn.execute("DELETE FROM samples WHERE ts < ?", (now - int(self.s["retention_days"] * 86400),))
                self._last_prune = now

    # ------------------------------------------------------------------ work
    def _work(self, worker, start, end):
        """Accepted work of `worker` in [start, end]: (difficulty, shares, connections, avg vdiff, last seen).
        `worker` may also be a list of workers, measured together."""
        names = list(worker) if isinstance(worker, (list, tuple, set)) else [worker]
        cond = "worker IN (%s)" % ",".join("?" * len(names))
        with self.db() as conn:
            rows = conn.execute(
                "SELECT ts, skey, session_start, shares, vdiff FROM samples"
                f" WHERE {cond} AND ts >= ? AND ts <= ? ORDER BY skey, ts",
                (*names, int(start - 2 * self.poll), int(end + self.poll))).fetchall()
            last = conn.execute(f"SELECT MAX(ts) FROM samples WHERE {cond}", names).fetchone()[0]
        work = 0.0
        shares = 0
        prev = {}
        vd = []
        for r in rows:
            k = r["skey"]
            if k not in prev:
                # Born inside the window: count from zero. Otherwise only the increment.
                base = 0 if r["session_start"] >= start - self.poll else r["shares"]
            else:
                base = prev[k]
            prev[k] = r["shares"]
            inc = max(r["shares"] - base, 0)
            shares += inc
            work += inc * (r["vdiff"] or 0.0)
            if r["vdiff"]:
                vd.append(r["vdiff"])
        return work, shares, len(prev), (sum(vd) / len(vd) if vd else None), last

    def first_seen(self, worker, since):
        with self.db() as conn:
            return conn.execute("SELECT MIN(ts) FROM samples WHERE worker = ? AND ts >= ?",
                                (worker, int(since))).fetchone()[0]

    def analyse(self, rental, worker, observe_from, shared):
        now = time.time()
        start = rental["start_unix"]
        end = rental["end_unix"]
        with self.db() as conn:
            oldest = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now
        obs = observe_from or 0
        if obs:
            # After a worker change the rig needs a moment to reconnect under the new name.
            seen = self.first_seen(worker, obs)
            if seen and seen - obs < 900:
                obs = seen - self.poll
        win_start = max(start, oldest, obs)
        win_end = min(end, now)
        elapsed = max(win_end - win_start, 1.0)
        work, shares, conns, avg_vd, last = self._work(worker, win_start, win_end)
        delivered = work * HASHES_PER_DIFF / elapsed / 1e12
        promised = rental["advertised_th"]
        vdiff = avg_vd or 4096.0
        expected = promised * 1e12 * elapsed / (HASHES_PER_DIFF * vdiff)
        sigma = math.sqrt(expected) if expected > 0 else 0.0
        z = (expected - shares) / sigma if sigma else None
        if shared:
            verdict = "shared"
        elif not last:
            verdict = "waiting"
        elif expected < 10:
            verdict = "starting"
        elif shares == 0:
            verdict = "nothing"
        elif z > self.s["sigma"]:
            verdict = "short"
        elif z < -self.s["sigma"]:
            verdict = "over"
        else:
            verdict = "ok"
        return {
            "worker": worker if isinstance(worker, str) or worker is None else ",".join(worker), "shared": shared,
            "observed_from": int(win_start), "partial": win_start > start + 2 * self.poll,
            "elapsed_h": elapsed / 3600, "delivered_th": delivered,
            "pct": delivered / promised * 100 if promised else None,
            "shares": shares, "expected_shares": expected, "z": z,
            "rel_err_pct": 100 / math.sqrt(shares) if shares else None,
            "connections": conns, "last_seen": last,
            "connected": bool(last and now - last < self.s["offline_after_s"]),
            "verdict": verdict,
        }

    def series(self, workers):
        """TH/s per worker in buckets, last `series_hours` hours (complete buckets only)."""
        bucket = int(self.s["series_bucket_s"])
        now = int(time.time())
        t0 = now - int(self.s["series_hours"] * 3600)
        t0 -= t0 % bucket
        n = (now - t0) // bucket            # the running bucket is left out: it is still filling
        out = {}
        if not workers or n < 1:
            return {"t0": t0, "bucket": bucket, "series": out}
        q = ",".join("?" * len(workers))
        with self.db() as conn:
            rows = conn.execute(
                "SELECT ts, skey, session_start, worker, shares, vdiff FROM samples"
                " WHERE ts >= ? AND worker IN (%s) ORDER BY skey, ts" % q,
                [t0 - 2 * self.poll] + list(workers)).fetchall()
        prev = {}
        for r in rows:
            k = r["skey"]
            base = prev.get(k)
            if base is None:
                base = 0 if r["session_start"] >= t0 - self.poll else None
            prev[k] = r["shares"]
            if base is None or r["ts"] < t0:
                continue
            i = (r["ts"] - t0) // bucket
            if i >= n:
                continue
            arr = out.setdefault(r["worker"], [0.0] * n)
            arr[i] += max(r["shares"] - base, 0) * (r["vdiff"] or 0.0)
        for w, arr in out.items():
            out[w] = [round(v * HASHES_PER_DIFF / bucket / 1e12, 3) for v in arr]
        return {"t0": t0, "bucket": bucket, "series": out}

    # ------------------------------------------------------------------ MRR side
    def workers(self):
        with self.db() as conn:
            return {r["rental_id"]: dict(r) for r in conn.execute("SELECT * FROM rental_worker")}

    def sync(self, api, rentals):
        """Read each active rental's pool worker; give rentals that share one a worker of their own."""
        pools = {}
        for r in rentals:
            p = api.call("GET", f"/rental/{r['id']}/pool")
            top = sorted(p.get("pools") or [], key=lambda x: int(x.get("priority") or 0))
            pools[str(r["id"])] = top[0] if top else None

        def worker_of(pool):
            user = (pool or {}).get("user") or ""
            return user.split(".", 1)[1].strip() if "." in user else None

        count = {}
        for pool in pools.values():
            w = worker_of(pool)
            count[w] = count.get(w, 0) + 1
        known = self.workers()
        now = int(time.time())
        renamed = []
        for rid, pool in pools.items():
            w = worker_of(pool)
            addr = ((pool or {}).get("user") or "").split(".", 1)[0] or None
            obs = (known.get(rid) or {}).get("observe_from")
            if self.s["assign_workers"] and pool and (not w or count.get(w, 0) > 1):
                want = f"{self.s['worker_prefix']}{rid}"
                set_pool_user(api, rid, pool, addr, want)
                renamed.append({"rental": rid, "from": w, "to": want, "at": now})
                w, obs = want, now
            self.remember(rid, w, obs, addr, now)
        self.track(api, rentals, now)
        with _lock:
            _status.update(ok=True, at=now, error=None, renamed=(_status["renamed"] + renamed)[-20:])
        return renamed

    # ------------------------------------------------------------------ history and favourites
    def track(self, api, rentals, now):
        """Remember active rentals; when one is no longer active, judge it whole and keep the result."""
        import json
        import mrr_bot
        live = {}
        for r in rentals:
            if r.get("ended"):
                continue
            try:
                live[str(r["id"])] = {
                    "id": str(r["id"]), "rig_id": str(r["rig"]["id"]), "rig_name": r["rig"].get("name"),
                    "advertised_th": mrr_bot.to_th(r["hashrate"]["advertised"]),
                    "start_unix": int(mrr_bot.parse_mrr_time(r["start"]).timestamp()),
                    "end_unix": int(mrr_bot.parse_mrr_time(r["end"]).timestamp()),
                }
            except (KeyError, TypeError, ValueError):
                continue
        with self.db() as conn:
            gone = [dict(json.loads(row["data"])) for row in conn.execute("SELECT rental_id, data FROM active")
                    if row["rental_id"] not in live]
            for rid, d in live.items():
                conn.execute("INSERT INTO active VALUES (?,?,?) ON CONFLICT(rental_id) DO UPDATE"
                             " SET data=excluded.data, seen=excluded.seen", (rid, json.dumps(d), now))
        for d in gone:
            try:
                self.finish(api, d, now)
            except Exception:
                continue        # try again on the next pass
            with self.db() as conn:
                conn.execute("DELETE FROM active WHERE rental_id=?", (d["id"],))
        with self.db() as conn:
            conn.execute("DELETE FROM history WHERE finished < ?", (now - int(self.s["history_days"] * 86400),))

    def finish(self, api, d, now):
        import mrr_bot
        full = api.call("GET", f"/rental/{d['id']}")
        avg = (full.get("hashrate") or {}).get("average") or {}
        rig = full.get("rig") or {}
        d["rig_id"] = str(rig.get("id") or d["rig_id"])
        d["rig_name"] = rig.get("name") or d["rig_name"]
        try:
            d["end_unix"] = int(mrr_bot.parse_mrr_time(full["end"]).timestamp())
        except (KeyError, TypeError, ValueError):
            pass
        k = self.workers().get(d["id"]) or {}
        w = k.get("worker")
        a = self.analyse(d, w, k.get("observe_from"), False) if w else None
        price = full.get("price") or {}
        row = (d["id"], d["rig_id"], d["rig_name"], d["start_unix"], d["end_unix"], d["advertised_th"],
               mrr_bot.to_th(avg) if avg else None, a and a["delivered_th"], a and a["pct"],
               a["verdict"] if a else "none", a and a["shares"], a and a["expected_shares"],
               str(price.get("paid") or ""), price.get("currency"), k.get("address"), w, now)
        with self.db() as conn:
            conn.execute("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            if a and a["pct"] is not None and a["verdict"] in ("ok", "over") and a["pct"] >= self.s["favorite_pct"]:
                f = conn.execute("SELECT * FROM favorites WHERE rig_id=?", (d["rig_id"],)).fetchone()
                n = (f["rentals"] if f else 0) + 1
                avg_pct = ((f["avg_pct"] or 0) * (n - 1) + a["pct"]) / n if f else a["pct"]
                conn.execute("INSERT OR REPLACE INTO favorites VALUES (?,?,?,?,?,?)",
                             (d["rig_id"], d["rig_name"], n, avg_pct, a["pct"], now))

    def history(self):
        with self.db() as conn:
            hist = [dict(r) for r in conn.execute("SELECT * FROM history ORDER BY finished DESC")]
            favs = [dict(r) for r in conn.execute("SELECT * FROM favorites ORDER BY avg_pct DESC")]
        return {"history": hist, "favorites": favs, "history_days": self.s["history_days"],
                "favorite_pct": self.s["favorite_pct"]}

    def favorite(self, rig_id, name=None, on=True):
        with self.db() as conn:
            if on:
                conn.execute("INSERT OR IGNORE INTO favorites VALUES (?,?,0,NULL,NULL,?)",
                             (str(rig_id), name, int(time.time())))
            else:
                conn.execute("DELETE FROM favorites WHERE rig_id=?", (str(rig_id),))

    def remember(self, rid, worker, observe_from, address, now=None):
        with self.db() as conn:
            conn.execute("INSERT INTO rental_worker (rental_id, worker, observe_from, updated, address) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(rental_id) DO UPDATE SET worker=excluded.worker, observe_from=excluded.observe_from,"
                         " updated=excluded.updated, address=excluded.address",
                         (str(rid), worker, observe_from, int(now or time.time()), address))

    def view(self, rentals):
        known = self.workers()
        active = [r for r in rentals if not r.get("ended")]
        count = {}
        for r in active:
            w = (known.get(str(r["id"])) or {}).get("worker")
            count[w] = count.get(w, 0) + 1
        out = []
        for r in active:
            k = known.get(str(r["id"])) or {}
            w = k.get("worker")
            a = self.analyse(r, w, k.get("observe_from"), count.get(w, 0) > 1) if w else None
            out.append({"id": r["id"], "rig_id": r.get("rig_id"), "rig_name": r.get("rig_name"), "advertised_th": r["advertised_th"],
                        "mrr_average_th": r.get("average_th"), "mrr_average_pct": r.get("average_pct"),
                        "left_h": r.get("left_h"), "address": k.get("address"), "analysis": a,
                        "length_h": r.get("length_h"), "max_h": r.get("maxhours"), "extensions": r.get("extensions")})
        workers = sorted({x["analysis"]["worker"] for x in out if x["analysis"]})
        with _lock:
            st = dict(_status)
        return {"rentals": out, "series": self.series(workers), "sync": st,
                "assign_workers": bool(self.s["assign_workers"]), "sigma": self.s["sigma"]}


def top_pool(api, rid):
    """The rental's first-priority pool, as MRR returns it."""
    p = api.call("GET", f"/rental/{rid}/pool")
    top = sorted(p.get("pools") or [], key=lambda x: int(x.get("priority") or 0))
    return top[0] if top else None


def set_pool_user(api, rid, pool, address, worker):
    """Point a rental's pool user at <address>.<worker> (same host, port and password)."""
    api.call("PUT", f"/rental/{rid}/pool/{pool.get('priority') or 0}", body={
        "host": pool["host"], "port": int(pool["port"]), "user": f"{address}.{worker}",
        "pass": pool.get("pass") or "x",   # MRR rejects an empty password
    })


def sync_loop(audit, make_api, save):
    while True:
        try:
            api = make_api()
            try:
                audit.sync(api, api.active_rentals())
            finally:
                save(api)
        except Exception as e:  # never take the dashboard down
            with _lock:
                _status.update(ok=False, at=int(time.time()), error=str(e)[:200])
        time.sleep(audit.s["sync_seconds"])
