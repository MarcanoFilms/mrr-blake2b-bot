"""PyBLØCK Market rentals for the MRR Command Center.

Reads the public PyBLØCK Market API (packages, order status) and creates orders
ONLY when the owner presses Rent and confirms on the dashboard. Nothing here
pays anything: an order returns an invoice (or an on-chain address + amount)
that the owner pays from his own wallet.

The order id IS the order's secret (whoever has it can see and manage the
order), so it lives only in a local state file (mode 600, gitignored), is never
logged in full and never leaves through /api/overview. Orders are referred to
by a local random `ref` instead.

config.json:
  "pyblock": {
    "enabled": true,
    "api": "https://b.pyblock.xyz:8443/api/market/v1.php",
    "gateway": {"host": "", "port": 0},     # public stratum of YOUR DATUM gateway
    "backup":  {"host": "", "port": 0},     # optional second gateway
    "address": "",                          # payout address ("" = rent_address, then the MRR profile's)
    "worker_prefix": "pyb"                  # gateway user = address.<prefix><6 hex>
  }
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = {
    "enabled": True,
    "api": "https://b.pyblock.xyz:8443/api/market/v1.php",
    "gateway": {"host": "", "port": 0},
    "backup": {"host": "", "port": 0},
    "address": "",
    "worker_prefix": "pyb",
}
UA = "mrr-command-center/1.0"
PACKAGES_TTL = 120           # s: their catalog changes slowly
GWTEST_TTL = 60              # s: /payout/gateway-test is limited to 12/min
POLL_PENDING = 15            # s between status reads while an invoice waits to be paid
POLL_LIVE = 60               # s between status reads while a rental runs
DRAFT_MAX_AGE = 3600         # s: an order that never got an id is dropped after this
PAID = {"paid", "awaiting_review", "submitting", "active"}
FINAL = {"completed", "expired", "cancelled", "failed", "not_found"}
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ADDRESS_RE = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
WORKER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
UNPAID_LABEL = "factura no pagada (caducó)"
STATE_LABELS = {   # Spanish names shown on the dashboard and in notices
    "draft": "sin enviar", "pending_payment": "esperando pago", "paid": "pagado", "awaiting_review": "en revisión",
    "submitting": "arrancando", "active": "activo", "completed": "terminado", "expired": "caducado",
    "cancelled": "cancelado", "failed": "fallido", "not_found": "no encontrado",
}


class OrderError(Exception):
    """An order the API refused. `retryable` means the same client_nonce may be sent again."""

    def __init__(self, msg, status=None, retryable=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def settings(c):
    p = dict(DEFAULTS)
    p.update(c.get("pyblock") or {})
    for k in ("gateway", "backup"):
        p[k] = dict(DEFAULTS[k], **(p.get(k) or {}))
    return p


def mask(secret):
    """What may appear in logs instead of an order id."""
    s = str(secret or "")
    return (s[:4] + "…") if s else ""


# ----------------------------------------------------------------------------- parsing
def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def sat_th_day(total_sats, th, hours):
    th, hours, total = _num(th), _num(hours), _num(total_sats)
    if not th or not hours or total is None:
        return None
    return total / th / (hours / 24.0)


def parse_packages(raw, mrr_ref=None):
    """Normalise /rentals/packages. `mrr_ref`: MRR's sats/TH/day to compare with (or None)."""
    if not isinstance(raw, dict) or not raw.get("ok", True):
        raise RuntimeError(_err_text(raw) or "PyBLØCK packages: unexpected answer")
    pkgs = []
    for p in raw.get("packages") or []:
        hours = _num(p.get("hours"), 0)
        unit = _num(p.get("unit_th"), 0)
        steps = []
        for s in p.get("steps") or []:
            th = _num(s.get("th"), unit * int(s.get("qty") or 1))
            st = {"qty": int(s.get("qty") or 1), "th": th, "total_sats": int(_num(s.get("total_sats"), 0)),
                  "sat_th_day": sat_th_day(s.get("total_sats"), th, hours)}
            st["vs_mrr"] = (st["sat_th_day"] / mrr_ref - 1) if (mrr_ref and st["sat_th_day"]) else None
            steps.append(st)
        if not steps and p.get("total_sats") is not None:
            steps.append({"qty": 1, "th": unit, "total_sats": int(_num(p["total_sats"], 0)),
                          "sat_th_day": sat_th_day(p["total_sats"], unit, hours), "vs_mrr": None})
        first = steps[0] if steps else {}
        pkgs.append({
            "id": str(p.get("id") or ""), "hours": hours, "unit_th": unit,
            "max_qty": int(p.get("max_qty") or len(steps) or 1), "available": bool(p.get("available")),
            "total_sats": int(_num(p.get("total_sats"), first.get("total_sats") or 0)),
            "sat_th_day": first.get("sat_th_day"), "vs_mrr": first.get("vs_mrr"), "steps": steps,
        })
    pkgs.sort(key=lambda x: (not x["available"], x["sat_th_day"] if x["sat_th_day"] is not None else 1e18))
    promo = raw.get("promo") or None
    if promo and not promo.get("discount_pct"):
        promo = None
    return {"packages": pkgs, "promo": promo, "limits": raw.get("limits") or {},
            "pay_methods": list(raw.get("pay_methods") or ["ln"]), "delivery": raw.get("delivery") or "",
            "now": raw.get("now"), "mrr_ref": mrr_ref}


def compare(cat, mrr_ref):
    """A copy of the catalog with each price set against MRR's (sats/TH/day, same unit)."""
    cat = json.loads(json.dumps(cat))
    cat["mrr_ref"] = mrr_ref
    for p in cat["packages"]:
        for s in p["steps"]:
            s["vs_mrr"] = (s["sat_th_day"] / mrr_ref - 1) if (mrr_ref and s["sat_th_day"]) else None
        p["vs_mrr"] = p["steps"][0]["vs_mrr"] if p["steps"] else None
    return cat


def renew_price(cat, unit_th, hours, qty=1):
    """sats/TH/day of the cheapest package on sale now with the same unit size and hours
    (the step with the same quantity when there is one), or None."""
    unit_th, hours = _num(unit_th), _num(hours)
    if not cat or not unit_th or not hours:
        return None
    best = None
    for p in cat.get("packages") or []:
        if not p.get("available") or abs((p.get("unit_th") or 0) - unit_th) > 1e-6 or abs((p.get("hours") or 0) - hours) > 1e-6:
            continue
        steps = [s for s in p.get("steps") or [] if s.get("qty") == qty] or list(p.get("steps") or [])
        for st in steps:
            v = st.get("sat_th_day")
            if v is not None and (best is None or v < best):
                best = v
    return best


def _pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _err_text(raw):
    if not isinstance(raw, dict):
        return ""
    if raw.get("error"):
        return str(raw["error"])
    errs = raw.get("errors")
    if isinstance(errs, list) and errs:
        return "; ".join(str(e) for e in errs)
    if isinstance(errs, str):
        return errs
    return ""


def parse_order(raw, now=None):
    """Normalise an order answer (POST /rentals/orders or GET /rentals/orders/{id}).

    The fields are read defensively: the answer may be flat or wrap the order in
    "order", and payment data may sit at the top level or under "payment"."""
    now = now or time.time()
    o = raw.get("order") if isinstance(raw.get("order"), dict) else raw
    pay = o.get("payment") if isinstance(o.get("payment"), dict) else {}
    src = [o, pay, raw]

    def g(*keys):
        for d in src:
            v = _pick(d, *keys)
            if v is not None:
                return v
        return None

    out = {
        "order_id": g("order_id", "id", "secret"),
        "status": str(g("status") or "").lower() or None,
        "status_url": g("status_url"), "tracking_url": g("tracking_url"),
        "pay_method": g("pay_method", "pay"),
        "invoice": g("invoice", "bolt11", "payment_request", "xbt_ln_invoice", "ln_invoice"),
        "xbt_address": g("xbt_address", "address_xbt"),
        "xbt_amount": g("xbt_amount", "amount_xbt"),
        "xbt_seen": g("xbt_seen"), "xbt_remaining": g("xbt_remaining"),
        "xbt_confs": g("xbt_confs"), "xbt_confs_needed": g("xbt_confs_needed"),
        "total_sats": _num(g("total_sats", "amount_sats")),
        "still_payable": g("still_payable"),
        "percent_done": _num(g("percent_done")),
    }
    # an on-chain order's "address" is ours (payout), never confuse it with the payment address
    exp = _num(g("expires_at"))
    left = _num(g("seconds_left", "expires_in"))
    out["expires_at"] = exp if exp else (now + left if left is not None else None)
    th = _num(g("hashrate_ths"))
    if th is None and _num(g("hashrate_phs")) is not None:
        th = _num(g("hashrate_phs")) * 1000
    live = _num(g("live_ths"))
    if live is None and _num(g("live_hashrate_phs")) is not None:
        live = _num(g("live_hashrate_phs")) * 1000
    out["hashrate_ths"], out["live_ths"] = th, live
    rem = _num(g("remaining_secs", "bid_remaining_est_secs"))
    out["ends_at"] = (now + rem) if rem is not None else None
    out["hours"] = _num(g("duration_h", "hours"))
    out["started_at"] = _num(g("started_at", "bid_started_at"))
    out["created_at"] = _num(g("created_at"))
    out["kind"] = g("kind")
    # where the hashrate goes, when the answer says it (the v1 API does not, so far):
    # the stratum user (address.worker) and/or the payout address (possibly masked "bc1q…xyz")
    gwu = o.get("gateway") if isinstance(o.get("gateway"), dict) else {}
    out["gateway_user"] = _pick(gwu, "user") or g("stratum_user", "pool_user", "gateway_user")
    out["payout_address"] = g("btc_address", "payout_address") or (o.get("address") if isinstance(o.get("address"), str) else None)
    return out


def extract_order_id(text):
    """The order id from what the owner pastes: the id itself or PyBLØCK's tracking link."""
    t = str(text or "").strip()
    if "order=" in t:
        q = urllib.parse.urlparse(t).query if "://" in t else t.split("?", 1)[-1]
        t = (urllib.parse.parse_qs(q).get("order") or [""])[0].strip()
    elif "/" in t:
        t = t.rstrip("/").rsplit("/", 1)[-1]
    return t if ORDER_ID_RE.match(t) else None


def address_matches(pattern, address):
    """Full address, or PyBLØCK's masked form 'bc1qabc…wxyz' (prefix … suffix)."""
    pattern, address = str(pattern or ""), str(address or "")
    if not pattern or not address:
        return False
    for sep in ("…", "..."):
        if sep in pattern:
            pre, _, suf = pattern.partition(sep)
            return len(pre) >= 6 and len(suf) >= 4 and address.startswith(pre) and address.endswith(suf)
    return pattern == address


def is_unpaid(rec):
    """An order whose invoice ran out without being paid (PyBLØCK calls that 'expired'
    too): nothing was ever delivered, so it is not a rental that ended."""
    if rec.get("status") != "expired":
        return False
    paid = (rec.get("notified") or {}).get("paid") or rec.get("started_at") or (rec.get("percent_done") or 0) > 0
    return not paid


# ----------------------------------------------------------------------------- HTTP
def http_json(method, url, body=None, timeout=25):
    """(status, json) — HTTP errors with a JSON body are returned, not raised."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"ok": False, "error": f"HTTP {e.code}"}


# ----------------------------------------------------------------------------- the store
class PyBlock:
    def __init__(self, here, cfg_fn, notify=None, gateway_clients=None, mrr_ref=None, address_fallback=None,
                 analyse=None):
        self.path = os.path.join(here, "pyblock.json")
        self.cfg_fn = cfg_fn
        self.notify = notify or (lambda c, text: None)
        self.gateway_clients = gateway_clients or (lambda: [])
        self.mrr_ref = mrr_ref or (lambda: None)
        self.address_fallback = address_fallback or (lambda: "")
        # analyse(worker, start, end, th) -> the audit's verdict dict (shares x vdiff), or None
        self.analyse = analyse or (lambda *a: None)
        self.lock = threading.RLock()
        self._pk = {"at": 0, "data": None, "error": None}
        self._gw = {"at": 0, "data": None}
        self._addr = {"at": 0, "value": ""}
        self.state = self._load()

    # -- persistence (600: the order ids are secrets)
    def _load(self):
        try:
            with open(self.path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        st.setdefault("orders", [])
        return st

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def settings(self):
        return settings(self.cfg_fn())

    def base(self):
        return self.settings()["api"].rstrip("/")

    # -- payout address
    def address(self):
        p, c = self.settings(), self.cfg_fn()
        a = (p.get("address") or "").strip() or (c.get("rent_address") or "").strip()
        if a:
            return a, "config"
        if time.time() - self._addr["at"] > 600 or not self._addr["value"]:
            try:
                self._addr.update(at=time.time(), value=(self.address_fallback() or "").strip())
            except Exception:
                self._addr["at"] = time.time()
        return self._addr["value"], "perfil MRR"

    # -- catalog
    def packages(self, force=False):
        with self.lock:
            if not force and self._pk["data"] and time.time() - self._pk["at"] < PACKAGES_TTL:
                return self._pk["data"], self._pk["error"]
        try:
            status, raw = http_json("GET", self.base() + "/rentals/packages", timeout=20)
            if status >= 400:
                raise RuntimeError(_err_text(raw) or f"HTTP {status}")
            data, err = parse_packages(raw, self.mrr_ref()), None
            data["at"] = time.time()
        except Exception as e:
            data, err = None, f"PyBLØCK: {e}"[:200]
        with self.lock:
            if data:
                self._pk = {"at": time.time(), "data": data, "error": None}
            else:
                self._pk.update(at=time.time(), error=err)
            return self._pk["data"], self._pk["error"]

    def gateway_test(self, force=False):
        """Their own gateway check (public host, BLAKE2b chain, DATUM), without creating anything."""
        if not force and self._gw["data"] and time.time() - self._gw["at"] < GWTEST_TTL:
            return self._gw["data"]
        p = self.settings()
        gw = p["gateway"]
        addr, _ = self.address()
        if not gw.get("host") or not gw.get("port"):
            return {"ok": False, "error": "falta pyblock.gateway (host y port) en config.json"}
        if not addr:
            return {"ok": False, "error": "no hay address de cobro (rent_address o perfil de MRR)"}
        q = urllib.parse.urlencode({"address": addr, "pool": f"{gw['host']}:{int(gw['port'])}"})
        try:
            status, raw = http_json("GET", self.base() + "/payout/gateway-test?" + q, timeout=45)
            res = {"ok": bool(raw.get("ok")), "error": _err_text(raw) or (None if raw.get("ok") else f"HTTP {status}"),
                   "gateway": raw.get("gateway")}
        except Exception as e:
            res = {"ok": False, "error": f"no se pudo consultar a PyBLØCK: {e}"[:200]}
        res["at"] = time.time()
        self._gw = {"at": time.time(), "data": res}
        return res

    # -- orders
    def _find(self, ref=None, nonce=None):
        for o in self.state["orders"]:
            if (ref and o.get("ref") == ref) or (nonce and o.get("nonce") == nonce):
                return o
        return None

    def create(self, package, qty, pay, nonce):
        """Create (or fetch again, same nonce) an order. Raises OrderError."""
        p = self.settings()
        if not p.get("enabled", True):
            raise OrderError("PyBLØCK está desactivado (pyblock.enabled)")
        if not NONCE_RE.match(str(nonce or "")):
            raise OrderError("client_nonce inválido")
        with self.lock:
            rec = self._find(nonce=nonce)
            if rec and rec.get("order_id"):
                return self.public(rec)          # same nonce, already created: nothing is sent again
        gw, bk = p["gateway"], p["backup"]
        if not gw.get("host") or not int(gw.get("port") or 0):
            raise OrderError("falta pyblock.gateway (host y port) en config.json")
        addr, _ = self.address()
        if not addr or not ADDRESS_RE.match(addr):
            raise OrderError("no hay una address de cobro válida (pyblock.address, rent_address o perfil de MRR)")
        # PyBLØCK rebuilds its catalogue (the ids change, e.g. p14x24 -> p24x24): read it
        # fresh before ordering, so an id shown a while ago is not sent stale.
        cat, err = self.packages(force=True)
        if not cat:
            raise OrderError(err or "no se pudo leer el catálogo de PyBLØCK", retryable=True)
        pk = next((x for x in cat["packages"] if x["id"] == package), None)
        if not pk:
            raise OrderError(f"el paquete {package} ya no está en el catálogo de PyBLØCK "
                             "(lo han cambiado): recarga la tabla y elige de nuevo")
        qty = int(qty)
        step = next((s for s in pk["steps"] if s["qty"] == qty), None)
        if qty < 1 or qty > pk["max_qty"] or not step:
            raise OrderError(f"cantidad fuera de rango (1–{pk['max_qty']})")
        if pay not in cat["pay_methods"]:
            raise OrderError(f"método de pago no admitido: {pay}")
        with self.lock:
            rec = self._find(nonce=nonce)
            if not rec:
                if not pk["available"]:
                    raise OrderError("ese paquete está agotado ahora mismo")
                rec = {"ref": secrets.token_hex(4), "nonce": nonce, "created": time.time(), "status": "draft",
                       "package": pk["id"], "qty": qty, "th": step["th"], "hours": pk["hours"],
                       "total_sats": step["total_sats"], "pay": pay, "address": addr,
                       "worker": f"{p['worker_prefix']}{secrets.token_hex(3)}",
                       "gateway": f"{gw['host']}:{int(gw['port'])}",
                       "backup": f"{bk['host']}:{int(bk['port'])}" if bk.get("host") and bk.get("port") else "",
                       "notified": {}}
                self.state["orders"].append(rec)
                self._save()
        # unit_th + hours instead of the package id: the API takes either, and on 14 nov 2026
        # it answered "invalid package id" to every id with a decimal size (p4_5x24, p16_1x48…)
        # while unit_th=4.5 + hours=24 for the same package passed.
        body = {"address": rec["address"], "qty": rec["qty"], "pay": rec["pay"],
                "unit_th": pk["unit_th"], "hours": pk["hours"],
                "client_nonce": nonce,
                "gateway": {"host": gw["host"], "port": int(gw["port"]), "user": f"{rec['address']}.{rec['worker']}"}}
        if rec["backup"]:
            body["backup"] = {"host": bk["host"], "port": int(bk["port"]), "user": f"{rec['address']}.{rec['worker']}"}
        try:
            status, raw = http_json("POST", self.base() + "/rentals/orders", body, timeout=60)
        except Exception as e:
            with self.lock:
                rec["error"] = f"sin respuesta de PyBLØCK: {e}"[:200]
                self._save()
            raise OrderError(rec["error"] + " — puedes reintentar: es el mismo pedido", retryable=True)
        if status >= 400 or not raw.get("ok", True):
            msg = _err_text(raw) or f"HTTP {status}"
            retry = status == 429 or status >= 500
            if status == 422:
                msg = "PyBLØCK no acepta el gateway: " + msg
            elif status == 429:
                msg = "PyBLØCK limita a 6 pedidos por minuto; espera un poco y reintenta (es el mismo pedido)"
            with self.lock:
                if retry:
                    rec["error"] = msg[:300]
                else:
                    self.state["orders"].remove(rec)
                self._save()
            raise OrderError(msg, status=status, retryable=retry)
        info = parse_order(raw)
        if not info.get("order_id"):
            with self.lock:
                rec["error"] = "PyBLØCK respondió sin order_id"
                self._save()
            raise OrderError(rec["error"], retryable=True)
        with self.lock:
            self._apply(rec, info, raw_status=status)
            rec.pop("error", None)
            rec["status"] = info["status"] or "pending_payment"
            self._save()
        if not rec["notified"].get("created"):
            self._notice(rec, "created")
        return self.public(rec)

    def _status_extra(self, order_id):
        """PyBLØCK's own tracking page reads api/hashrate_status.php, which also gives the
        payout address (masked). Best effort: None when it is not there."""
        base = self.base()
        if "/market/" not in base:
            return None
        url = base.split("/market/", 1)[0] + "/hashrate_status.php?id=" + urllib.parse.quote(order_id)
        try:
            status, raw = http_json("GET", url, timeout=15)
        except Exception:
            return None
        if status >= 400 or not isinstance(raw, dict) or not raw.get("ok"):
            return None
        return parse_order(raw)

    def add_external(self, text, worker=""):
        """Follow an order created outside the panel (PyBLØCK's web or app, any payment
        method): read it, check it is a rental and keep it like the others. `worker`
        (optional) is the stratum worker it mines with, or address.worker."""
        order_id = extract_order_id(text)
        if not order_id:
            raise OrderError("pega el identificador del pedido (o el enlace de seguimiento de PyBLØCK)")
        worker = str(worker or "").strip()
        w_addr = ""
        if "." in worker:
            w_addr, _, worker = worker.partition(".")
            if not ADDRESS_RE.match(w_addr):
                raise OrderError("el worker va como nombre o como dirección.nombre")
        if worker and not WORKER_RE.match(worker):
            raise OrderError("nombre de worker no válido (letras, números, _ o -)")
        with self.lock:
            if any(o.get("order_id") == order_id for o in self.state["orders"]):
                raise OrderError("ese pedido ya está en el panel")
        try:
            status, raw = http_json("GET", f"{self.base()}/rentals/orders/{urllib.parse.quote(order_id)}", timeout=20)
        except Exception as e:
            raise OrderError(f"sin respuesta de PyBLØCK: {e}"[:200], retryable=True)
        if status == 404:
            raise OrderError("PyBLØCK no conoce ese pedido (revisa que esté entero)")
        if status >= 400 or not isinstance(raw, dict) or not raw.get("ok"):
            raise OrderError("PyBLØCK no lo acepta: " + (_err_text(raw) or f"HTTP {status}"),
                             status=status, retryable=status == 429 or status >= 500)
        info = parse_order(raw)
        if not info.get("status") or not info.get("hashrate_ths") or not info.get("hours"):
            raise OrderError("eso no parece un alquiler de hashrate de PyBLØCK (sin TH/s u horas)")
        if info.get("kind") and str(info["kind"]).lower() not in ("order", "rental"):
            raise OrderError(f"eso no es un pedido de alquiler (tipo {info['kind']})")
        extra = self._status_extra(order_id) or {}
        gw_user = info.get("gateway_user") or extra.get("gateway_user") or ""
        if not worker and "." in str(gw_user):
            w_addr, _, worker = str(gw_user).partition(".")
        addr = w_addr or info.get("payout_address") or extra.get("payout_address") or ""
        th, hours = info["hashrate_ths"], info["hours"]
        rec = {"ref": secrets.token_hex(4), "external": True, "order_id": order_id,
               "created": info.get("created_at") or time.time(), "added": time.time(),
               "status": info["status"], "package": "externo", "qty": 1, "th": th, "hours": hours,
               "total_sats": int(info.get("total_sats") or 0), "pay": info.get("pay_method") or "?",
               "address": addr, "worker": worker or None,
               "match": "worker" if worker else ("address" if addr else "none"),
               "gateway": "", "backup": "", "notified": {}}
        if info.get("started_at"):
            rec["started_at"] = info["started_at"]
        with self.lock:
            if any(o.get("order_id") == order_id for o in self.state["orders"]):
                raise OrderError("ese pedido ya está en el panel")
            self._apply(rec, info)
            if rec["status"] in PAID:
                rec["notified"]["paid"] = time.time()     # paid before it got here: no "paid" notice later
            if rec["status"] in FINAL:
                rec["notified"]["ended"] = time.time()
            self.state["orders"].append(rec)
            self._save()
        self._notice(rec, "added")
        return self.public(rec)

    def _apply(self, rec, info, raw_status=None):
        for k in ("order_id", "status_url", "tracking_url"):
            if info.get(k):
                rec[k] = info[k]
        pay = {k: info.get(k) for k in ("invoice", "xbt_address", "xbt_amount", "xbt_seen", "xbt_remaining",
                                        "xbt_confs", "xbt_confs_needed", "expires_at", "still_payable")}
        if any(v is not None for v in pay.values()):
            rec["payment"] = dict(rec.get("payment") or {}, **{k: v for k, v in pay.items() if v is not None})
        for k in ("hashrate_ths", "live_ths", "percent_done", "ends_at", "started_at"):
            if info.get(k) is not None:
                rec[k] = info[k]
        if info.get("status"):
            rec["status"] = info["status"]
        rec["checked"] = time.time()

    def refresh(self, rec, force=False):
        """Read one order's status from PyBLØCK (rate-limited by our own cadence)."""
        if not rec.get("order_id") or (rec.get("status") in FINAL and not force):
            return rec
        due = POLL_PENDING if rec.get("status") in (None, "pending_payment") else POLL_LIVE
        if not force and time.time() - (rec.get("checked") or 0) < due:
            return rec
        if force and time.time() - (rec.get("checked") or 0) < 5:
            return rec
        before = rec.get("status")
        try:
            status, raw = http_json("GET", f"{self.base()}/rentals/orders/{rec['order_id']}", timeout=20)
        except Exception as e:
            with self.lock:
                rec["checked"], rec["error"] = time.time(), f"sin respuesta: {e}"[:160]
                self._save()
            return rec
        with self.lock:
            if status == 404:
                rec["misses"] = rec.get("misses", 0) + 1
                rec["checked"] = time.time()
                if rec["misses"] >= 3:
                    rec["status"] = "not_found"
            elif status >= 400:
                rec["checked"], rec["error"] = time.time(), (_err_text(raw) or f"HTTP {status}")[:160]
            else:
                rec.pop("error", None)
                rec["misses"] = 0
                self._apply(rec, parse_order(raw))
            self._save()
        after = rec.get("status")
        if after != before:
            if after in PAID and not rec["notified"].get("paid"):
                self._notice(rec, "paid")
            if after in FINAL and not rec["notified"].get("ended"):
                self._notice(rec, "ended")
        return rec

    def poll_all(self):
        with self.lock:
            now = time.time()
            # drop drafts that never got an order id (they cannot be followed anyway)
            keep = [o for o in self.state["orders"] if o.get("order_id") or now - o.get("created", now) < DRAFT_MAX_AGE]
            if len(keep) != len(self.state["orders"]):
                self.state["orders"] = keep
                self._save()
            todo = [o for o in self.state["orders"] if o.get("order_id") and o.get("status") not in FINAL]
        for o in todo:
            self.refresh(o)

    def loop(self, every=10):
        while True:
            try:
                self.poll_all()
                self.packages()
            except Exception as e:   # never let a bad answer kill the thread
                print(f"[pyblock poll failed: {e}]", flush=True)
            time.sleep(every)

    def forget(self, ref):
        with self.lock:
            rec = self._find(ref=ref)
            if not rec:
                raise OrderError("no existe")
            if rec.get("order_id") and rec.get("status") not in FINAL and not rec.get("external"):
                raise OrderError("solo se pueden quitar pedidos terminados o sin enviar")
            self.state["orders"].remove(rec)
            self._save()

    def link(self, ref):
        rec = self._find(ref=ref)
        return (rec or {}).get("tracking_url") or (rec or {}).get("status_url")

    # -- notices (no secrets: no order id, no invoice)
    def _notice(self, rec, kind):
        name = "pedido externo" if rec.get("external") else f"{rec['qty']}× {rec['package']}"
        what = f"{name} · {rec['th']:g} TH/s × {rec['hours']:g} h · {rec['total_sats']:,} sats ({rec['pay']})"
        text = {
            "added": f"➕ PyBLØCK: pedido hecho fuera del panel añadido ({STATE_LABELS.get(rec.get('status'), rec.get('status'))}) — {what}",
            "created": f"🟧 PyBLØCK: pedido creado, falta pagarlo — {what}",
            "paid": f"✅ PyBLØCK: alquiler pagado ({STATE_LABELS.get(rec.get('status'), rec.get('status'))}) — {what}",
            "ended": f"🏁 PyBLØCK: alquiler {STATE_LABELS.get(rec.get('status'), rec.get('status'))} — {what}",
        }[kind]
        if kind == "ended" and is_unpaid(rec):
            text = f"⌛ PyBLØCK: la factura caducó sin pagar — {what}"
        rec["notified"][kind] = time.time()
        with self.lock:
            self._save()
        try:
            self.notify(self.cfg_fn(), text)
        except Exception:
            pass

    # -- views (never the order id, nonce or the secret urls)
    def gateway_th(self, worker):
        if not worker:
            return None
        try:
            return sum(c.get("hashrate_th") or 0 for c in self.gateway_clients() if c.get("worker") == worker)
        except Exception:
            return None

    def address_workers(self, rec):
        """For an external order without a worker of its own: the gateway connections that
        pay to its address and come from PyBLØCK (their rDNS or user agent), minus the
        workers of the other orders here and MRR's. An estimate, not a per-order measure."""
        if rec.get("match") != "address" or not rec.get("address"):
            return []
        with self.lock:
            others = {o.get("worker") for o in self.state["orders"] if o is not rec and o.get("worker")}
        mrr_re = re.compile(r"^mrr\d+$")
        out = []
        try:
            clients = self.gateway_clients()
        except Exception:
            return []
        for c in clients:
            w = c.get("worker") or ""
            if not c.get("subscribed") or not address_matches(rec["address"], c.get("address")):
                continue
            if w in others or mrr_re.match(w):
                continue
            if c.get("label") == "PyBLOCK rental" or "pyblock" in str(c.get("useragent") or "").lower():
                out.append(c)
        return out

    def public(self, rec):
        out = {k: rec.get(k) for k in ("ref", "created", "status", "package", "qty", "th", "hours", "total_sats",
                                       "pay", "address", "worker", "gateway", "backup", "hashrate_ths", "live_ths",
                                       "percent_done", "ends_at", "checked", "error", "added", "started_at")}
        out["status_label"] = STATE_LABELS.get(rec.get("status"), rec.get("status"))
        out["unpaid"] = is_unpaid(rec)
        if out["unpaid"]:
            out["status_label"] = UNPAID_LABEL
        out["external"] = bool(rec.get("external"))
        out["match"] = rec.get("match") or ("worker" if rec.get("worker") else "none")
        out["sent"] = bool(rec.get("order_id"))
        out["has_link"] = bool(rec.get("tracking_url") or rec.get("status_url"))
        out["sat_th_day"] = sat_th_day(rec.get("total_sats"), rec.get("th"), rec.get("hours"))
        if out["match"] == "address":
            aw = self.address_workers(rec)
            out["gateway_th"] = sum(c.get("hashrate_th") or 0 for c in aw)
            out["address_workers"] = sorted({c.get("worker") or "" for c in aw})
        else:
            out["gateway_th"] = self.gateway_th(rec.get("worker"))
        if rec.get("status") in (None, "draft", "pending_payment") or (rec.get("payment") or {}).get("still_payable"):
            out["payment"] = rec.get("payment")
        ends = rec.get("ends_at")
        if ends and rec.get("status") in PAID:
            out["left_h"] = max(0.0, (ends - time.time()) / 3600)
        out["live"] = rec.get("status") in PAID
        return out

    def view(self, force=False):
        p = self.settings()
        cat, err = self.packages(force=force)
        if cat:
            cat = compare(cat, self.mrr_ref())
        addr, src = self.address()
        with self.lock:
            recs = sorted(self.state["orders"], key=lambda o: -(o.get("created") or 0))
            orders = [self.public(o) for o in recs]
        for rec, o in zip(recs, orders):
            qty = int(rec.get("qty") or 1)
            o["renew_sat_th_day"] = renew_price(cat, (rec.get("th") or 0) / qty, rec.get("hours"), qty)
            o["analysis"] = None
            ends, hours = rec.get("ends_at"), rec.get("hours")
            who = rec.get("worker")
            if o["match"] == "address":
                who = o.get("address_workers") or None
            if o["live"] and ends and hours and who:
                # start: PyBLØCK's started_at when it gives it, else end - length
                start = rec.get("started_at") or (ends - hours * 3600)
                try:
                    o["analysis"] = self.analyse(who, start, ends, rec.get("th"))
                    if o["analysis"] and o["match"] == "address":
                        o["analysis"]["approx"] = True
                except Exception as e:
                    o["analysis"] = None
                    print(f"[pyblock analyse failed: {e}]", flush=True)
        live = [o for o in orders if o["live"]]
        return {
            "enabled": bool(p.get("enabled", True)), "error": err, "catalog": cat,
            "gateway": f"{p['gateway']['host']}:{int(p['gateway']['port'] or 0)}" if p["gateway"].get("host") else "",
            "backup": f"{p['backup']['host']}:{int(p['backup']['port'] or 0)}" if p["backup"].get("host") else "",
            "address": addr, "address_source": src, "orders": orders,
            "totals": {"active": len(live), "th": sum(o["th"] or 0 for o in live),
                       "live_ths": sum(o["live_ths"] or 0 for o in live),
                       "gateway_th": sum(o["gateway_th"] or 0 for o in live),
                       # measured since each start (shares x vdiff), like "llega al gateway" for MRR
                       "delivered_th": sum((o["analysis"] or {}).get("delivered_th") or 0 for o in live)},
            "gateway_test": self._gw["data"],
        }
