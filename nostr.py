"""Just enough Nostr for P2Hash, in the standard library only.

The PyBLØCK hashrate market on Nostr (P2Hash) speaks:
  * NIP-01 events, signed with BIP-340 Schnorr signatures over secp256k1;
  * NIP-44 v2 encryption (ECDH + HKDF + ChaCha20 + HMAC-SHA256);
  * NIP-17 private messages inside NIP-59 gift wraps (kind 14 -> 13 -> 1059);
  * NIP-47 Nostr Wallet Connect (kind 23194 / 23195) to pay BOLT12 offers;
over plain WebSocket connections to relays.

None of that is in Python's standard library and the MRR Command Center has no
other dependency, so this module carries a small implementation of each. It is
written for clarity, not speed: a signature takes a few milliseconds, which is
plenty for a handful of messages per minute. The tests check it against
vectors produced by nostr-tools (the library the PyBLØCK agent itself uses).

Secret keys never leave this process: they are 32-byte values kept in the
panel's own state file (mode 600) or in config.json (the NWC URI).
"""
import base64
import hashlib
import hmac
import json
import queue
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.parse

# ----------------------------------------------------------------------------- secp256k1
P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_INF = (0, 1, 0)   # Jacobian point at infinity (Z == 0)


def _jdouble(p):
    x, y, z = p
    if z == 0 or y == 0:
        return _INF
    yy = y * y % P
    s = 4 * x * yy % P
    m = 3 * x * x % P
    x3 = (m * m - 2 * s) % P
    return x3, (m * (s - x3) - 8 * yy * yy) % P, 2 * y * z % P


def _jadd(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _jdouble(p) if s1 == s2 else _INF
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    return x3, (r * (v - x3) - s1 * hhh) % P, h * z1 * z2 % P


def _affine(p):
    if p[2] == 0:
        return None
    zi = pow(p[2], -1, P)
    zi2 = zi * zi % P
    return p[0] * zi2 % P, p[1] * zi2 * zi % P


def _mul(pt, k):
    """k * pt (pt affine (x, y)), double-and-add."""
    k %= N
    acc = _INF
    q = (pt[0], pt[1], 1)
    while k:
        if k & 1:
            acc = _jadd(acc, q)
        q = _jdouble(q)
        k >>= 1
    return acc


_GTABLE = []


def _gmul(k):
    """k * G with a table of 2^i * G (only additions)."""
    if not _GTABLE:
        q = (GX, GY, 1)
        for _ in range(256):
            _GTABLE.append(q)
            q = _jdouble(q)
    k %= N
    acc = _INF
    i = 0
    while k:
        if k & 1:
            acc = _jadd(acc, _GTABLE[i])
        k >>= 1
        i += 1
    return acc


def _lift_x(x):
    if not 0 < x < P:
        return None
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        return None
    return x, (y if y % 2 == 0 else P - y)


def _tagged(tag, *parts):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + b"".join(parts)).digest()


def _b32(i):
    return i.to_bytes(32, "big")


def _sk_int(sk):
    d = int.from_bytes(bytes.fromhex(sk) if isinstance(sk, str) else sk, "big")
    if not 0 < d < N:
        raise ValueError("invalid secret key")
    return d


def gen_sk():
    while True:
        b = secrets.token_bytes(32)
        if 0 < int.from_bytes(b, "big") < N:
            return b.hex()


def pubkey(sk):
    """x-only public key (hex) of a secret key (hex or bytes)."""
    return _b32(_affine(_gmul(_sk_int(sk)))[0]).hex()


def schnorr_sign(msg32, sk, aux=None):
    d0 = _sk_int(sk)
    px, py = _affine(_gmul(d0))
    d = d0 if py % 2 == 0 else N - d0
    aux = aux if aux is not None else secrets.token_bytes(32)
    t = _b32(d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big"))
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t, _b32(px), msg32), "big") % N
    if k0 == 0:
        raise ValueError("bad nonce")
    rx, ry = _affine(_gmul(k0))
    k = k0 if ry % 2 == 0 else N - k0
    e = int.from_bytes(_tagged("BIP0340/challenge", _b32(rx), _b32(px), msg32), "big") % N
    return _b32(rx) + _b32((k + e * d) % N)


def schnorr_verify(msg32, pub, sig):
    try:
        pub = bytes.fromhex(pub) if isinstance(pub, str) else pub
        sig = bytes.fromhex(sig) if isinstance(sig, str) else sig
    except ValueError:
        return False
    if len(pub) != 32 or len(sig) != 64:
        return False
    pt = _lift_x(int.from_bytes(pub, "big"))
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if pt is None or r >= P or s >= N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32], pub, msg32), "big") % N
    R = _affine(_jadd(_gmul(s), _mul(pt, N - e)))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def ecdh_x(sk, pub):
    """Shared secret of NIP-44: the x coordinate of sk * lift_x(pub)."""
    pt = _lift_x(int.from_bytes(bytes.fromhex(pub), "big"))
    if pt is None:
        raise ValueError("invalid public key")
    return _b32(_affine(_mul(pt, _sk_int(sk)))[0])


# ----------------------------------------------------------------------------- ChaCha20 (RFC 8439)
def _rotl(v, c):
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _chacha_block(key_words, counter, nonce_words):
    s = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574, *key_words, counter, *nonce_words]
    x = list(s)
    for _ in range(10):
        for a, b, c, d in ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
                           (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)):
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF
            x[d] = _rotl(x[d] ^ x[a], 16)
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF
            x[b] = _rotl(x[b] ^ x[c], 12)
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF
            x[d] = _rotl(x[d] ^ x[a], 8)
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF
            x[b] = _rotl(x[b] ^ x[c], 7)
    return struct.pack("<16I", *((x[i] + s[i]) & 0xFFFFFFFF for i in range(16)))


def chacha20(key, nonce, data, counter=0):
    kw = struct.unpack("<8I", key)
    nw = struct.unpack("<3I", nonce)
    out = bytearray(len(data))
    for i in range(0, len(data), 64):
        block = _chacha_block(kw, (counter + i // 64) & 0xFFFFFFFF, nw)
        chunk = data[i:i + 64]
        out[i:i + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
    return bytes(out)


# ----------------------------------------------------------------------------- NIP-44 v2
def conversation_key(sk, pub):
    return hmac.new(b"nip44-v2", ecdh_x(sk, pub), hashlib.sha256).digest()


def _hkdf_expand(prk, info, length):
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def _padded_len(n):
    if n <= 32:
        return 32
    nxt = 1 << ((n - 1).bit_length())
    chunk = 32 if nxt <= 256 else nxt // 8
    return chunk * ((n - 1) // chunk + 1)


def nip44_encrypt(plaintext, ckey, nonce=None):
    raw = plaintext.encode()
    if not 1 <= len(raw) <= 65535:
        raise ValueError("NIP-44: message length out of range")
    nonce = nonce or secrets.token_bytes(32)
    k = _hkdf_expand(ckey, nonce, 76)
    padded = struct.pack(">H", len(raw)) + raw + b"\x00" * (_padded_len(len(raw)) - len(raw))
    ct = chacha20(k[:32], k[32:44], padded)
    mac = hmac.new(k[44:], nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ct + mac).decode()


def nip44_decrypt(payload, ckey):
    if not payload or payload[0] == "#":
        raise ValueError("NIP-44: unknown version")
    data = base64.b64decode(payload)
    if len(data) < 99 or data[0] != 2:
        raise ValueError("NIP-44: unknown version or too short")
    nonce, ct, mac = data[1:33], data[33:-32], data[-32:]
    k = _hkdf_expand(ckey, nonce, 76)
    if not hmac.compare_digest(mac, hmac.new(k[44:], nonce + ct, hashlib.sha256).digest()):
        raise ValueError("NIP-44: invalid MAC")
    padded = chacha20(k[:32], k[32:44], ct)
    n = struct.unpack(">H", padded[:2])[0]
    if n == 0 or len(padded) != 2 + _padded_len(n):
        raise ValueError("NIP-44: invalid padding")
    return padded[2:2 + n].decode()


# ----------------------------------------------------------------------------- events
def _serial(ev):
    return json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                      separators=(",", ":"), ensure_ascii=False)


def event_id(ev):
    return hashlib.sha256(_serial(ev).encode()).hexdigest()


def finalize(ev, sk):
    """Fill pubkey, id and sig of an event dict {kind, created_at, tags, content}."""
    ev = dict(ev)
    ev.setdefault("created_at", int(time.time()))
    ev.setdefault("tags", [])
    ev["pubkey"] = pubkey(sk)
    ev["id"] = event_id(ev)
    ev["sig"] = schnorr_sign(bytes.fromhex(ev["id"]), sk).hex()
    return ev


def verify_event(ev):
    try:
        return (isinstance(ev, dict) and ev.get("id") == event_id(ev)
                and schnorr_verify(bytes.fromhex(ev["id"]), ev["pubkey"], ev["sig"]))
    except (KeyError, TypeError, ValueError):
        return False


def tag(ev, key):
    for t in ev.get("tags") or []:
        if isinstance(t, list) and len(t) > 1 and t[0] == key:
            return t[1]
    return None


# ----------------------------------------------------------------------------- NIP-17 / NIP-59
TWO_DAYS = 2 * 24 * 3600


def _random_past():
    return int(time.time() - secrets.randbelow(TWO_DAYS))


def wrap_dm(sender_sk, recipient_pk, content):
    """A NIP-17 private message (kind 14 rumor) sealed (13) and gift-wrapped (1059)."""
    rumor = {"pubkey": pubkey(sender_sk), "created_at": int(time.time()), "kind": 14,
             "tags": [["p", recipient_pk]], "content": content}
    rumor["id"] = event_id(rumor)
    seal = finalize({"kind": 13, "created_at": _random_past(), "tags": [],
                     "content": nip44_encrypt(json.dumps(rumor), conversation_key(sender_sk, recipient_pk))}, sender_sk)
    eph = gen_sk()
    return finalize({"kind": 1059, "created_at": _random_past(), "tags": [["p", recipient_pk]],
                     "content": nip44_encrypt(json.dumps(seal), conversation_key(eph, recipient_pk))}, eph)


def unwrap_dm(wrap, sk):
    """(sender pubkey, rumor) of a gift wrap addressed to sk, or None. The seal must be
    signed by the same key the rumor claims to come from."""
    try:
        if wrap.get("kind") != 1059:
            return None
        seal = json.loads(nip44_decrypt(wrap["content"], conversation_key(sk, wrap["pubkey"])))
        if seal.get("kind") != 13 or not verify_event(seal):
            return None
        rumor = json.loads(nip44_decrypt(seal["content"], conversation_key(sk, seal["pubkey"])))
        if rumor.get("pubkey") != seal["pubkey"]:
            return None
        return seal["pubkey"], rumor
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None


# ----------------------------------------------------------------------------- WebSocket (RFC 6455 client)
class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=10):
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("relay URL must be ws:// or wss://")
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        self.sock = sock
        self.wlock = threading.Lock()
        self._buf = b""
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        hostport = host if u.port in (None, 80, 443) else f"{host}:{port}"
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"User-Agent: mrr-command-center\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSClosed("handshake: connection closed")
            head += chunk
            if len(head) > 65536:
                raise WSClosed("handshake: header too long")
        head, _, self._buf = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WSClosed("handshake refused: " + lines[0][:80])
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        acc = [l.split(":", 1)[1].strip() for l in lines[1:] if l.lower().startswith("sec-websocket-accept:")]
        if acc != [want]:
            raise WSClosed("handshake: bad accept key")

    def _frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.wlock:
            self.sock.sendall(head + mask + body)

    def send(self, text):
        self._frame(0x1, text.encode())

    def ping(self):
        self._frame(0x9, b"")

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """The next text message. Raises socket.timeout when nothing came in time."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 8 * 1024 * 1024:
                raise WSClosed("frame too large")
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise WSClosed("closed by relay")
            if op == 0x9:
                self._frame(0xA, data)
                continue
            if op == 0xA:
                continue
            if op in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------- relays
class Relay:
    """One relay with automatic reconnection (1 s -> 60 s). Subscriptions are sent again
    after a reconnection; publish() waits for the relay's OK (or times out)."""

    def __init__(self, url, log=None, connect=WebSocket):
        self.url = url
        self.log = log or (lambda *a: None)
        self._connect = connect
        self.subs = {}          # id -> (filter, on_event, on_eose)
        self.waiting = {}       # event id -> [threading.Event, ok, message]
        self.open = False
        self.closed = False
        self.ws = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"relay {url}")
        self.thread.start()

    def _run(self):
        backoff = 1
        while not self.closed:
            try:
                ws = self._connect(self.url)
                ws.sock.settimeout(30)
                self.ws, self.open, self.error, backoff = ws, True, None, 1
                for sid, (flt, _, _) in list(self.subs.items()):
                    ws.send(json.dumps(["REQ", sid, flt]))
                while not self.closed:
                    try:
                        msg = ws.recv()
                    except socket.timeout:
                        ws.ping()
                        continue
                    self._dispatch(msg)
            except Exception as e:     # noqa: BLE001 - any failure: reconnect
                self.error = str(e)[:160]
            self.open = False
            try:
                if self.ws:
                    self.ws.close()
            except Exception:
                pass
            if self.closed:
                break
            time.sleep(backoff)
            backoff = min(60, backoff * 2)

    def _dispatch(self, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if not isinstance(m, list) or not m:
            return
        if m[0] == "EVENT" and len(m) >= 3 and m[1] in self.subs:
            try:
                self.subs[m[1]][1](m[2], self.url)
            except Exception as e:     # a bad handler must not drop the connection
                self.log(f"handler error on {self.url}: {e}")
        elif m[0] == "EOSE" and len(m) >= 2 and m[1] in self.subs and self.subs[m[1]][2]:
            try:
                self.subs[m[1]][2](self.url)
            except Exception:
                pass
        elif m[0] == "OK" and len(m) >= 3 and m[1] in self.waiting:
            w = self.waiting[m[1]]
            w[1], w[2] = bool(m[2]), (m[3] if len(m) > 3 else "")
            w[0].set()

    def send(self, obj):
        if self.open and self.ws:
            try:
                self.ws.send(json.dumps(obj))
                return True
            except OSError:
                return False
        return False

    def sub(self, sid, flt, on_event, on_eose=None):
        self.subs[sid] = (flt, on_event, on_eose)
        self.send(["REQ", sid, flt])

    def unsub(self, sid):
        if self.subs.pop(sid, None):
            self.send(["CLOSE", sid])

    def publish(self, ev, timeout=8):
        w = [threading.Event(), False, "timeout"]
        self.waiting[ev["id"]] = w
        if not self.send(["EVENT", ev]):
            self.waiting.pop(ev["id"], None)
            return False, "not connected"
        w[0].wait(timeout)
        self.waiting.pop(ev["id"], None)
        return w[1], w[2]

    def close(self):
        self.closed = True
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


class Pool:
    """Several relays; events delivered once (by id)."""

    def __init__(self, urls, log=None, connect=WebSocket):
        self.relays = [Relay(u, log, connect) for u in urls]
        self._seen = {}
        self._lock = threading.Lock()

    def sub(self, sid, flt, on_event):
        def once(ev, url):
            eid = ev.get("id") if isinstance(ev, dict) else None
            with self._lock:
                if not eid or eid in self._seen:
                    if eid in self._seen:
                        self._seen[eid].add(url)
                    return
                self._seen[eid] = {url}
                if len(self._seen) > 20000:
                    self._seen = dict(list(self._seen.items())[-5000:])
            on_event(ev, url)
        for r in self.relays:
            r.sub(sid, flt, once)

    def relays_of(self, eid):
        with self._lock:
            return set(self._seen.get(eid) or ())

    def publish(self, ev, timeout=8):
        """Publish on every relay in parallel: [(url, ok, message)]."""
        out = []

        def one(r):
            ok, msg = r.publish(ev, timeout)
            out.append((r.url, ok, msg))
        ts = [threading.Thread(target=one, args=(r,), daemon=True) for r in self.relays]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout + 2)
        return out

    def status(self):
        return [{"url": r.url, "open": r.open, "error": r.error} for r in self.relays]

    def close(self):
        for r in self.relays:
            r.close()


# ----------------------------------------------------------------------------- NIP-47 (Nostr Wallet Connect)
def parse_nwc(uri):
    """{wallet_pk, relays, secret} of a nostr+walletconnect:// URI (ValueError if it is not one)."""
    uri = str(uri or "").strip()
    if not uri.startswith(("nostr+walletconnect://", "nostrwalletconnect://")):
        raise ValueError("no es una URI nostr+walletconnect://")
    u = urllib.parse.urlparse(uri.replace("nostr+walletconnect://", "http://", 1)
                              .replace("nostrwalletconnect://", "http://", 1))
    q = urllib.parse.parse_qs(u.query)
    wpk = (u.hostname or u.path.strip("/")).lower()
    secret = (q.get("secret") or [""])[0].lower()
    relays = q.get("relay") or []
    if len(wpk) != 64 or len(secret) != 64 or not relays:
        raise ValueError("URI NWC incompleta (pubkey, relay y secret)")
    int(wpk, 16), _sk_int(secret)
    return {"wallet_pk": wpk, "relays": relays, "secret": secret}


class NWC:
    """A wallet connection. request(method, params) -> {"result": …} or {"error": {code, message}}."""

    def __init__(self, uri, log=None, connect=WebSocket):
        c = parse_nwc(uri)
        self.wpk, self.sk = c["wallet_pk"], c["secret"]
        self.pk = pubkey(self.sk)
        self.ck = conversation_key(self.sk, self.wpk)
        self.wait = {}
        self.pool = Pool(c["relays"][:1], log, connect)
        self.pool.sub("nwc", {"kinds": [23195], "authors": [self.wpk], "#p": [self.pk],
                              "since": int(time.time()) - 10}, self._on)

    def _on(self, ev, _url):
        rid = tag(ev, "e")
        q = self.wait.get(rid)
        if not q or ev.get("pubkey") != self.wpk or not verify_event(ev):
            return
        try:
            q.put(json.loads(nip44_decrypt(ev["content"], self.ck)))
        except (ValueError, KeyError) as e:
            q.put({"error": {"code": "DECRYPT", "message": str(e)}})

    def ready(self, timeout=10):
        t = time.time() + timeout
        while time.time() < t:
            if any(r.open for r in self.pool.relays):
                return True
            time.sleep(0.2)
        return False

    def request(self, method, params=None, timeout=60):
        if not self.ready():
            return {"error": {"code": "RELAY", "message": "no se pudo conectar al relay de la billetera"}}
        ev = finalize({"kind": 23194, "created_at": int(time.time()),
                       "tags": [["p", self.wpk], ["encryption", "nip44_v2"]],
                       "content": nip44_encrypt(json.dumps({"method": method, "params": params or {}}), self.ck)},
                      self.sk)
        q = self.wait[ev["id"]] = queue.Queue()
        try:
            res = self.pool.publish(ev)
            if not any(ok for _, ok, _ in res):
                return {"error": {"code": "RELAY", "message": "el relay no aceptó la petición: "
                                  + "; ".join(str(m) for _, _, m in res)[:120]}}
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return {"error": {"code": "TIMEOUT", "message": f"sin respuesta de la billetera en {timeout} s"}}
        finally:
            self.wait.pop(ev["id"], None)

    def close(self):
        self.pool.close()


__all__ = ["pubkey", "gen_sk", "schnorr_sign", "schnorr_verify", "conversation_key", "nip44_encrypt",
           "nip44_decrypt", "finalize", "verify_event", "event_id", "wrap_dm", "unwrap_dm", "tag",
           "WebSocket", "Relay", "Pool", "NWC", "parse_nwc"]
