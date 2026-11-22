"""Tickets for MRR rentals that underdeliver, following MRR's own refund policy.

What MRR's API allows (checked against https://www.miningrigrentals.com/apidocv2 on
20 nov 2026): there is NO endpoint to open a support ticket, a dispute or a refund
request. Tickets are opened by hand, from the rental's page or the ticket centre.
The API does have the rental's message thread (PUT /rental/<id>/message), read by the
rig owner and MRR support, and the refund trail (the rental's `was_refunded` /
`refunded` and /account/transactions of type credit/refund).

MRR's policy (helpcenter/For-Renters/35): partial delivery is reviewed only once the
rental has ENDED, and a ticket must be opened within 12 hours of the end (then the owner
is paid). During a rental MRR asks renters to contact the owner first.

So there are three switches, all off by default:

  * enabled (ticket notice after the end): when an MRR rental has ended, is still inside
    the 12-hour window and its final delivery is below 100 % beyond statistical noise
    (shares received more than `sigma` standard deviations below what the paid hashrate
    should have produced, and below `max_pct`), and MRR has not refunded it, the evidence
    is kept and Kilobot sends the ticket text with the time left to claim. Once per rental.
    It looks `after_end_h` (8.5) after the end: MRR asks to wait up to 8 hours for its own
    review, and its automatic refunds arrive at the end or 7.5-8.7 hours later.
  * message_owner: the same evidence is also posted on the rental's message thread.
  * warn_early: while a rental runs and is clearly short (the old mid-rental test), the
    owner gets one early, polite message on the rental thread, as MRR asks.

Why a noise test and not a plain "< 100 %": shares arrive as a Poisson process, so a rig
delivering exactly 100 % shows a measured figure that wanders around it by about
1/sqrt(expected shares). Requiring the shortfall to exceed `sigma` (3) of those deviations
means a rig that delivered in full is wrongly flagged about once in 700 rentals.
"""
import math
import time

DEFAULTS = {
    # after the end
    "max_pct": 100.0,             # flag only below this % of what was paid (and beyond sigma)
    "sigma": None,                # None = the audit's sigma (3)
    "min_expected_shares": 200,   # enough shares for the test to mean something
    "after_end_h": 8.5,           # MRR reviews short rentals itself and refunds within ~8 h of the end
                                  # (measured 20 nov 2026: at the end or 7.5-8.7 h later): wait for it
    "window_h": 12.0,             # MRR's claim window after the end
    # early warning to the owner, while the rental runs
    "min_fraction": 0.25,         # part of the rental that must have elapsed
    "min_observed_h": 1.0,        # hours the audit must have watched the rental's own worker
    "early_max_pct": 95.0,        # only below this % (the sad face)
}
MRR = "https://www.miningrigrentals.com"
KV_KEY = "autoticket"
SWITCHES = ("enabled", "message_owner", "warn_early")


def settings(cfg):
    out = dict(DEFAULTS)
    out.update(((cfg.get("audit") or {}).get("tickets")) or {})
    if out["sigma"] is None:
        out["sigma"] = float((cfg.get("audit") or {}).get("sigma") or 3.0)
    return out


def switches(audit):
    v = audit.kv_get(KV_KEY, {}) or {}
    return {k: bool(v.get(k)) for k in SWITCHES}


def set_switches(audit, **kw):
    cur = switches(audit)
    for k in SWITCHES:
        if kw.get(k) is not None:
            cur[k] = bool(kw[k])
    audit.kv_set(KV_KEY, cur)
    return cur


def _fmt_utc(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "?"


def zscore(shares, expected):
    """How many standard deviations the received shares fall below the expected ones."""
    try:
        e = float(expected)
        return (e - float(shares)) / math.sqrt(e) if e > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- tests
def final_eligible(h, s, now):
    """(True, "") when a finished MRR rental in the history deserves its ticket notice."""
    if (h.get("source") or "mrr") != "mrr":
        return False, "not an MRR rental"
    end = h.get("end_unix") or 0
    if not end or now < end + s["after_end_h"] * 3600:
        return False, "not ended yet"
    if now >= end + s["window_h"] * 3600:
        return False, "MRR's 12-hour window is over"
    if h.get("delivered_th") is None or h.get("expected_shares") is None:
        return False, "no audit figures"
    if float(h["expected_shares"]) < s["min_expected_shares"]:
        return False, "too few shares to judge"
    z = zscore(h.get("shares") or 0, h["expected_shares"])
    if z is None or z <= s["sigma"]:
        return False, "within statistical noise"
    if h.get("pct") is None or float(h["pct"]) >= s["max_pct"]:
        return False, "not below the threshold"
    return True, ""


def early_eligible(r, a, s, now):
    """(True, "") when a running MRR rental is clearly short: the early warning to the owner."""
    if r.get("ended") or not (r.get("left_h") or 0) > 0:
        return False, "not active"
    if r.get("rig_online") is not True:
        return False, "rig not online at MRR"
    if not a or a.get("shared"):
        return False, "no worker of its own"
    if a.get("verdict") not in ("short", "nothing"):
        return False, "not short"
    paid = float(r.get("advertised_th") or 0)
    if paid <= 0 or a.get("delivered_th") is None or a["delivered_th"] >= paid * s["early_max_pct"] / 100:
        return False, "not below the threshold"
    start, end = r.get("start_unix") or 0, r.get("end_unix") or 0
    if end <= start or (now - start) / (end - start) < s["min_fraction"]:
        return False, "too early in the rental"
    if (a.get("elapsed_h") or 0) < s["min_observed_h"] or (a.get("expected_shares") or 0) < s["min_expected_shares"]:
        return False, "not enough audit data yet"
    return True, ""


eligible = early_eligible     # 1.4.0 name


def refunded(api, rental_id):
    """(True, why) if MRR already refunded the rental: its own flag or a credit/refund on it."""
    try:
        d = api.call("GET", f"/rental/{int(rental_id)}")
        d = d[0] if isinstance(d, list) and d else d
        if isinstance(d, dict) and (d.get("was_refunded") in (True, 1, "1", "yes", "true")
                                    or d.get("refunded") in (True, 1, "1", "yes", "true")):
            return True, "the rental shows as refunded"
    except Exception:
        pass
    t = api.call("GET", "/account/transactions", {"rental": int(rental_id), "type": "credit/refund", "limit": 20})
    for x in (t or {}).get("transactions") or []:
        if str(x.get("rental") or rental_id) == str(rental_id) and "refund" in str(x.get("type") or "").lower():
            try:
                if float(x.get("amount") or 0) > 0:
                    return True, f"credit/refund of {x.get('amount')}"
            except (TypeError, ValueError):
                continue
    return False, ""


# ---------------------------------------------------------------------------- texts
def from_history(h):
    """(rental, analysis) dicts for evidence() from a history row."""
    r = {"id": h["rental_id"], "rig_id": h.get("rig_id"), "rig_name": h.get("rig_name"),
         "advertised_th": h.get("advertised_th"), "paid": h.get("paid"), "currency": h.get("currency"),
         "length_h": round(((h.get("end_unix") or 0) - (h.get("start_unix") or 0)) / 3600, 2),
         "average_th": h.get("mrr_average_th"), "start_unix": h.get("start_unix"), "end_unix": h.get("end_unix"),
         "average_pct": (h["mrr_average_th"] / h["advertised_th"] * 100)
         if h.get("mrr_average_th") and h.get("advertised_th") else None}
    a = None
    if h.get("delivered_th") is not None:
        a = {"delivered_th": h["delivered_th"], "pct": h.get("pct"), "shares": h.get("shares"),
             "expected_shares": h.get("expected_shares"), "observed_from": h.get("start_unix"),
             "z": zscore(h.get("shares") or 0, h.get("expected_shares"))}
    return r, a


def evidence(r, a, now=None, finished=False):
    """English text for MRR (support reads English): what was paid, what MRR says, what arrived."""
    now = now or time.time()
    paid = float(r.get("advertised_th") or 0)
    lines = [f"Rental #{r.get('id')} (rig \"{r.get('rig_name') or ''}\" #{r.get('rig_id') or '?'}): "
             + ("it finished below the advertised hashrate." if finished else "it is delivering less than the advertised hashrate.")]
    lines.append(f"- Advertised (paid for): {paid:.2f} TH/s for {float(r.get('length_h') or 0):g} h"
                 + (f", paid {r.get('paid')} {r.get('currency') or ''}".rstrip() if r.get("paid") else "") + ".")
    if r.get("average_th") is not None:
        pct = f" ({float(r['average_pct']):.0f} %)" if r.get("average_pct") not in (None, "") else ""
        lines.append(f"- MRR's own average{'' if finished else ' so far'}: {float(r['average_th']):.2f} TH/s{pct}.")
    if a:
        z = a.get("z")
        lines.append(f"- Received at our DATUM gateway, counted from accepted shares (shares x share difficulty) on this"
                     f" rental's own worker since {_fmt_utc(a.get('observed_from'))}: {a['delivered_th']:.2f} TH/s"
                     f" ({(a.get('pct') or 0):.0f} %), {int(a.get('shares') or 0):,} accepted shares where the advertised"
                     f" hashrate should have produced {round(a.get('expected_shares') or 0):,}"
                     + (f" ({z:.1f} standard deviations below)" if z is not None and z > 0 else "") + ".")
    start, end = r.get("start_unix") or 0, r.get("end_unix") or 0
    if end > start:
        lines.append(f"- Rental window: {_fmt_utc(start)} to {_fmt_utc(end)}"
                     + ("" if finished else f"; {max(0.0, (now - start) / 3600):.1f} h elapsed") + ".")
    if not finished and r.get("rig_online") is not None:
        lines.append(f"- Rig online at MRR: {'yes' if r.get('rig_online') else 'no'}.")
    lines.append("Please check the rig. " + (
        "I ask for a review and a refund of the hashrate that was not delivered." if finished else
        "If it cannot deliver the advertised hashrate, I ask for the missing work to be made up"
        " or refunded when the rental ends."))
    return "\n".join(lines)


def _left(end, s, now):
    h = max(0.0, (end + s["window_h"] * 3600 - now) / 3600)
    return f"{int(h)} h {int((h % 1) * 60):02d} min"


def notice_final(h, text, s, now, message_sent, error=None):
    """Spanish Kilobot notice after the end, with the ticket text and the time left to claim."""
    rid = h["rental_id"]
    mrr = (f" · MRR dice {float(h['mrr_average_th']):.2f}" if h.get("mrr_average_th") is not None else "")
    z = zscore(h.get("shares") or 0, h.get("expected_shares"))
    msg = ("\nPruebas publicadas también en el hilo del alquiler (dueño y soporte)." if message_sent else
           ("\nNo se pudo escribir en el hilo del alquiler: " + str(error)[:120]) if error else "")
    return (f"🎫 MRR: el alquiler #{rid} \"{h.get('rig_name') or ''}\" terminó entregando menos de lo pagado\n"
            f"pagado {float(h.get('advertised_th') or 0):.2f} TH/s{mrr} · llegó al DATUM {float(h['delivered_th']):.2f} TH/s"
            f" ({float(h.get('pct') or 0):.1f} %) · {int(h.get('shares') or 0):,} de {round(h.get('expected_shares') or 0):,}"
            f" shares esperadas ({(z or 0):.1f} σ por debajo)\n"
            f"⏳ Te quedan {_left(h['end_unix'], s, now)} para abrir el ticket (MRR da 12 h tras terminar). MRR no lo ha reembolsado."
            f"{msg}\nÁbrelo aquí: {MRR}/rental/{rid}\nTexto para pegar:\n\n{text}")


def notice_early(r, a, message_sent, error=None):
    rid = r.get("id")
    what = ("mensaje enviado al dueño en el hilo del alquiler" if message_sent else
            "no se pudo escribir al dueño: " + str(error or "")[:120])
    return (f"📨 MRR: el alquiler #{rid} \"{r.get('rig_name') or ''}\" va corto "
            f"({a['delivered_th']:.2f} de {float(r.get('advertised_th') or 0):.2f} TH/s, {(a.get('pct') or 0):.0f} %): {what}.\n"
            f"Si al terminar sigue por debajo, el panel te avisará con el texto del ticket. {MRR}/rental/{rid}")


def early_text(r, a, now):
    return ("Hello, a heads-up from the renter: " + evidence(r, a, now).split(": ", 1)[1].replace(
        "Please check the rig. If it cannot deliver the advertised hashrate, I ask for the missing work to be made up"
        " or refunded when the rental ends.",
        "Could you please check the rig? Thank you."))


def _post(api_factory, rid, text):
    try:
        res = api_factory().call("PUT", f"/rental/{int(rid)}/message", body={"message": text})
        item = res[0] if isinstance(res, list) and res else res
        if isinstance(item, dict) and item.get("success") is False:
            return False, "MRR refused the message"
        return True, None
    except Exception as e:
        return False, str(e)[:300]


# ---------------------------------------------------------------------------- the pass
def check(audit, rentals, api_factory, notify, cfg, log=print, now=None):
    """One pass. Returns the keys acted on ("<id>" after the end, "<id>:early" mid-rental).

    `rentals` are the dashboard's active MRR rentals (with rig_online), `api_factory()` an MRR
    client, `notify(cfg, text)` the Kilobot notice. Nothing at all while the switches are off
    or the dashboard is read-only."""
    sw = switches(audit)
    if cfg.get("read_only") or not (sw["enabled"] or sw["warn_early"]):
        return []
    s = settings(cfg)
    now = now or time.time()
    done = []
    if sw["enabled"]:
        for h in audit.history()["history"]:
            ok, _ = final_eligible(h, s, now)
            if not ok or audit.ticket_seen(h["rental_id"]):
                continue
            try:
                was, why = refunded(api_factory(), h["rental_id"])
            except Exception as e:      # cannot tell: try again on the next pass
                log(f"[autoticket] rental {h['rental_id']}: refund check failed: {e}")
                continue
            r, a = from_history(h)
            text = evidence(r, a, now, finished=True)
            if was:
                audit.ticket_claim(h["rental_id"], now, "refunded", h.get("pct"), None, text)
                audit.ticket_update(h["rental_id"], None, "already refunded by MRR: " + why)
                log(f"[autoticket] rental {h['rental_id']}: {why}, no notice")
                continue
            if not audit.ticket_claim(h["rental_id"], now, h.get("verdict"), h.get("pct"),
                                      r.get("average_pct"), text):
                continue
            sent, err = (_post(api_factory, h["rental_id"], text) if sw["message_owner"] else (None, None))
            audit.ticket_update(h["rental_id"], sent, err)
            log(f"[autoticket] rental {h['rental_id']} ended at {h.get('pct') or 0:.1f}% of paid: notice sent,"
                f" owner message: {'sent' if sent else err or 'off'}")
            try:
                notify(cfg, notice_final(h, text, s, now, sent, err))
            except Exception:
                pass
            done.append(str(h["rental_id"]))
    if sw["warn_early"] and rentals:
        view = audit.view(rentals)
        by_id = {str(x["id"]): x for x in view["rentals"]}
        for r in rentals:
            a = (by_id.get(str(r.get("id"))) or {}).get("analysis")
            ok, _ = early_eligible(r, a, s, now)
            key = f"{r.get('id')}:early"
            if not ok or not audit.ticket_claim(key, now, a.get("verdict"), a.get("pct"),
                                                float(r["average_pct"]) if r.get("average_pct") not in (None, "") else None,
                                                early_text(r, a, now)):
                continue
            sent, err = _post(api_factory, r["id"], early_text(r, a, now))
            audit.ticket_update(key, sent, err)
            log(f"[autoticket] rental {r['id']} short mid-rental ({a.get('pct') or 0:.0f}%): early owner message"
                f" {'sent' if sent else err}")
            try:
                notify(cfg, notice_early(r, a, sent, err))
            except Exception:
                pass
            done.append(key)
    return done
