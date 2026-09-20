#!/usr/bin/env python3
"""pkgsender - send PKGs to a jailbroken PS5/PS4 from Linux and macOS.

Command-line counterpart of the Windows PKG Sender GUI. Same receiver
protocol (pkg-receiver.elf on port 12800, PS4 fallback 9090): the console
is told to install an http:// URL and pulls the bytes from a local
range-capable file server started by this tool.

Pure standard library, single file, no install step:

    ./pkgsender.py discover
    ./pkgsender.py send ~/pkgs/game.pkg
    ./pkgsender.py copy pkg-receiver.elf /data/homebrew
    ./pkgsender.py info ~/pkgs/game.pkg

Run `pkgsender.py --help` or `pkgsender.py <command> --help` for options.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

RECEIVER_PORTS = (12800, 9090)  # PS5 payload, PS4 fallback
BEACON_PORT = 12801
BEACON_MAGIC = b"PKGSENDER"
DEFAULT_SERVE_PORT = 9898
DEFAULT_REMOTE_DIR = "/data/homebrew"
DEFAULT_CHUNK = 2 * 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024  # receiver BODY_MAX
MIN_CHUNK = 64 * 1024
MAX_SWEEP_HOSTS = 4096

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ----------------------------------------------------------------- output --


class Out:
    """Console output honouring --quiet and non-tty (no \\r repaint)."""

    quiet = False
    tty = sys.stderr.isatty()
    _open_line = False

    @classmethod
    def info(cls, msg=""):
        if cls.quiet:
            return
        cls.endline()
        print(msg, file=sys.stderr, flush=True)

    @classmethod
    def warn(cls, msg):
        cls.endline()
        print("warning: " + msg, file=sys.stderr, flush=True)

    @classmethod
    def error(cls, msg):
        cls.endline()
        print("error: " + msg, file=sys.stderr, flush=True)

    @classmethod
    def status(cls, msg):
        """Transient single-line status (progress bars)."""
        if cls.quiet:
            return
        if not cls.tty:
            return
        width = shutil.get_terminal_size((80, 24)).columns
        line = msg[: max(0, width - 1)]
        sys.stderr.write("\r\033[K" + line)
        sys.stderr.flush()
        cls._open_line = True

    @classmethod
    def endline(cls):
        if cls._open_line:
            sys.stderr.write("\n")
            sys.stderr.flush()
            cls._open_line = False


def fmt_size(n):
    if n is None or n < 0:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return ("%d %s" if i == 0 else "%.2f %s") % (v, units[i])


def fmt_speed(bps):
    if bps <= 0:
        return "--"
    return fmt_size(bps) + "/s"


def fmt_eta(seconds):
    if seconds is None or seconds < 0 or seconds > 359999:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, sec) if h else "%02d:%02d" % (m, sec)


class Progress:
    """Rate-limited progress line with an EMA speed and ETA."""

    def __init__(self, label, total, width=24):
        self.label = label
        self.total = total
        self.width = width
        self.t0 = time.monotonic()
        self._last_t = self.t0
        self._last_bytes = 0
        self._speed = 0.0
        self._last_draw = 0.0

    def update(self, done, note="", force=False):
        now = time.monotonic()
        if not force and now - self._last_draw < 0.2:
            return
        self._last_draw = now
        dt = now - self._last_t
        if dt >= 0.5:
            inst = (done - self._last_bytes) / dt
            self._speed = inst if self._speed == 0 else self._speed * 0.7 + inst * 0.3
            self._last_t = now
            self._last_bytes = done
        pct = 100.0 if self.total <= 0 else min(100.0, done * 100.0 / self.total)
        filled = int(self.width * pct / 100.0)
        bar = "#" * filled + "-" * (self.width - filled)
        eta = None
        if self._speed > 0 and self.total > done:
            eta = (self.total - done) / self._speed
        tail = note or "%s / %s  %s  ETA %s" % (
            fmt_size(done), fmt_size(self.total), fmt_speed(self._speed), fmt_eta(eta))
        Out.status("%s [%s] %5.1f%%  %s" % (self.label, bar, pct, tail))

    def finish(self, note="done"):
        elapsed = max(0.001, time.monotonic() - self.t0)
        Out.endline()
        Out.info("%s  %s (%s in %s, avg %s)" % (
            self.label, note, fmt_size(self.total), fmt_eta(elapsed),
            fmt_speed(self.total / elapsed)))


# ----------------------------------------------------------------- config --


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "pkg-sender", "cli.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as ex:
        Out.warn("could not save config: %s" % ex)
        return False


# ------------------------------------------------------------------- net --


def local_ip_for(target):
    """PC address the console would reach us on (no packet is sent)."""
    for probe in (target, "8.8.8.8"):
        if not probe:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
        except Exception:
            continue
    return None


def lan_networks():
    """[(ifname, ip, prefixlen)] for active IPv4 NICs, real netmask."""
    nets = []
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                                 timeout=10).stdout
            iface = ""
            for line in out.splitlines():
                m = re.match(r"^(\S+):\s", line)
                if m:
                    iface = m.group(1)
                    continue
                m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", line)
                if m:
                    nets.append((iface, m.group(1), bin(int(m.group(2), 16)).count("1")))
        else:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if m:
                    nets.append((m.group(1), m.group(2), int(m.group(3))))
    except Exception:
        pass
    nets = [n for n in nets
            if not n[1].startswith("127.") and not n[1].startswith("169.254.")]
    if not nets:
        ip = local_ip_for(None)
        if ip:
            nets = [("?", ip, 24)]
    return nets


_VIRTUAL_MARKS = ("vmnet", "vboxnet", "utun", "tun", "tap", "docker", "br-",
                  "virbr", "wg", "tailscale", "ham")


def is_virtual(ifname):
    n = (ifname or "").lower()
    return any(n.startswith(m) for m in _VIRTUAL_MARKS)


def ip_to_int(ip):
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def int_to_ip(v):
    return "%d.%d.%d.%d" % ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255)


def hosts_of(ip, prefix, cap=MAX_SWEEP_HOSTS):
    """Usable hosts of a subnet, network/broadcast excluded, capped."""
    if prefix >= 31:
        return [ip]
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    net = ip_to_int(ip) & mask
    bcast = net | (~mask & 0xFFFFFFFF)
    out = []
    for h in range(net + 1, bcast):
        out.append(int_to_ip(h))
        if len(out) >= cap:
            break
    return out


def tcp_open(ip, port, timeout=0.3):
    try:
        s = socket.create_connection((ip, port), timeout)
        s.close()
        return True
    except Exception:
        return False


# --------------------------------------------------------------- receiver --


class ReceiverError(Exception):
    pass


class Receiver:
    """HTTP client for pkg-receiver.elf, sticky on the port that answers."""

    def __init__(self, ip, timeout=15):
        self.ip = ip
        self.timeout = timeout
        self._ports = list(RECEIVER_PORTS)
        self._sticky = self._ports[0]

    @property
    def port(self):
        return self._sticky

    def _ordered(self):
        yield self._sticky
        for p in self._ports:
            if p != self._sticky:
                yield p

    def _call(self, method, path, body=None, ctype=None, timeout=None):
        last = None
        for port in self._ordered():
            url = "http://%s:%d%s" % (self.ip, port, path)
            req = urllib.request.Request(url, data=body, method=method)
            if ctype:
                req.add_header("Content-Type", ctype)
            try:
                with _OPENER.open(req, timeout=timeout or self.timeout) as r:
                    text = r.read().decode("utf-8", "replace")
                self._sticky = port
                return text
            except urllib.error.HTTPError as ex:
                last = "HTTP %s" % ex.code
                try:
                    self._sticky = port
                    return ex.read().decode("utf-8", "replace")
                except Exception:
                    pass
            except Exception as ex:
                last = str(ex)
        raise ReceiverError(last or "no reply on %s" %
                            "/".join(str(p) for p in self._ports))

    def _get(self, path, timeout=None):
        return self._call("GET", path, timeout=timeout)

    def _post(self, path, obj=None, raw=None, timeout=None):
        if raw is not None:
            return self._call("POST", path, raw, "application/octet-stream", timeout)
        data = json.dumps(obj or {}).encode("utf-8")
        return self._call("POST", path, data, "application/json", timeout)

    @staticmethod
    def _json(text):
        try:
            v = json.loads(text)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _plain_error(text):
        """The payload answers some failures as 200 + `error:...` text."""
        t = (text or "").strip()
        return t[6:].strip() if t.lower().startswith("error:") else None

    # -- probes ------------------------------------------------------------

    def is_online(self, timeout=3):
        try:
            body = self._get("/api", timeout=timeout)
        except ReceiverError:
            return False
        return "Unsupported method" in body and "fail" in body

    def status(self, timeout=10):
        """(supported, dict) - old receivers answer HTML here."""
        try:
            body = self._get("/api/status", timeout=timeout)
        except ReceiverError:
            return False, {}
        j = self._json(body)
        if "busy" not in j:
            return False, {}
        return True, j

    def version(self, timeout=5):
        try:
            return self._json(self._get("/api/version", timeout=timeout)).get("build", "")
        except ReceiverError:
            return ""

    # -- install -----------------------------------------------------------

    def install(self, url, name=None, icon_url=None, timeout=20):
        payload = {"type": "direct",
                   "packages": [urllib.parse.quote(url.replace("https://", "http://"),
                                                   safe="")]}
        if name:
            payload["name"] = name
        if icon_url:
            payload["icon_url"] = icon_url
        body = self._post("/api/install", payload, timeout=timeout)
        j = self._json(body)
        if j.get("status") == "success":
            return True, body
        return False, j.get("error") or body.strip()

    # -- files -------------------------------------------------------------

    def stat(self, remote_path, timeout=10):
        body = self._get("/api/files/stat?path=" +
                         urllib.parse.quote(remote_path, safe=""), timeout=timeout)
        err = self._plain_error(body)
        if err:
            raise ReceiverError(err)
        j = self._json(body)
        return bool(j.get("exists")), int(j.get("size") or 0)

    def mkdir(self, remote_path, timeout=15):
        body = self._post("/api/files/mkdir", {"path": remote_path}, timeout=timeout)
        err = self._plain_error(body)
        if err:
            raise ReceiverError(err)
        return True

    def write(self, remote_path, offset, chunk, timeout=120):
        body = self._call(
            "POST",
            "/api/files/write?path=%s&offset=%d" % (
                urllib.parse.quote(remote_path, safe=""), offset),
            chunk, "application/octet-stream", timeout)
        err = self._plain_error(body)
        if err:
            raise ReceiverError(err)
        return True

    def done(self, remote_path, size, timeout=60):
        body = self._post("/api/files/done", {"path": remote_path, "size": size},
                          timeout=timeout)
        err = self._plain_error(body)
        if err:
            raise ReceiverError(err)
        return True


# -------------------------------------------------------------- discovery --


def listen_beacons(seconds, on_found=None):
    """Receiver beacons ("PKGSENDER v1" on UDP 12801). Returns [ip]."""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", BEACON_PORT))
        s.settimeout(0.5)
    except Exception as ex:
        Out.warn("cannot listen for beacons on UDP %d (%s)" % (BEACON_PORT, ex))
        return found
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(256)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data.startswith(BEACON_MAGIC):
                continue
            ip = addr[0]
            if ip.startswith("127.") or ip in found:
                continue
            found.append(ip)
            if on_found:
                on_found(ip)
    finally:
        s.close()
    return found


def sweep(nets, skip=(), workers=128, on_progress=None):
    """TCP sweep of every non-virtual subnet for the receiver ports."""
    targets = []
    for ifname, ip, prefix in nets:
        if is_virtual(ifname):
            continue
        for host in hosts_of(ip, prefix):
            if host != ip and host not in skip:
                targets.append(host)
    if on_progress:
        on_progress(len(targets))
    results = []
    lock = threading.Lock()
    index = [0]

    def worker():
        while True:
            with lock:
                i = index[0]
                index[0] += 1
            if i >= len(targets):
                return
            host = targets[i]
            if not (tcp_open(host, RECEIVER_PORTS[0]) or tcp_open(host, RECEIVER_PORTS[1])):
                continue
            rec = Receiver(host, timeout=3)
            entry = {"ip": host, "source": "sweep", "online": rec.is_online()}
            if entry["online"]:
                ok, st = rec.status(timeout=4)
                entry["busy"] = bool(st.get("busy")) if ok else None
            else:
                entry["source"] = "port-open"
                entry["busy"] = None
            with lock:
                results.append(entry)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(workers, max(1, len(targets))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(results, key=lambda e: ip_to_int(e["ip"]))


def find_consoles(beacon_seconds=3.0, do_sweep=True):
    """Beacon first, LAN sweep as fallback. Merged, deduped by IP."""
    merged = {}
    Out.info("Listening for receiver beacons (%.0fs)..." % beacon_seconds)
    for ip in listen_beacons(beacon_seconds,
                             on_found=lambda i: Out.info("  beacon from %s" % i)):
        rec = Receiver(ip, timeout=4)
        online = rec.is_online()
        ok, st = rec.status(timeout=4) if online else (False, {})
        merged[ip] = {"ip": ip, "source": "beacon" if online else "beacon-unverified",
                      "online": online,
                      "busy": bool(st.get("busy")) if ok else None}
    if merged and not do_sweep:
        return list(merged.values())
    if not merged:
        Out.info("No beacon heard - sweeping the LAN as fallback...")
    if do_sweep:
        nets = lan_networks()
        for ifname, ip, prefix in nets:
            if is_virtual(ifname):
                Out.info("  skipping virtual adapter %s" % ifname)
        for entry in sweep(nets, skip=set(merged),
                           on_progress=lambda n: Out.info("  scanning %d addresses..." % n)):
            merged.setdefault(entry["ip"], entry)
    return sorted(merged.values(), key=lambda e: ip_to_int(e["ip"]))


# ------------------------------------------------------------- pkg reader --


def _u32be(b, o):
    return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def _u32le(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


class Cnt:
    """Generic CNT container walker (shared PS4/PS5 package layout)."""

    def __init__(self, f, base, is_debug, is_meta, content_id, entries):
        self.f = f
        self.base = base
        self.is_debug = is_debug
        self.is_meta = is_meta
        self.content_id = content_id
        self.entries = entries  # [(id, name, flags, data_off, data_size)]

    @classmethod
    def open(cls, f, file_size):
        if file_size < 0x5A0:
            return None
        f.seek(0)
        magic = f.read(4)
        if magic == b"\x7fFIH":
            f.seek(0)
            fih = f.read(0x60)
            if len(fih) < 0x60:
                return None
            is_debug = fih[0x05] == 0x00
            is_meta = False
            emb = int.from_bytes(fih[0x58:0x60], "little")
            if emb == 0 or emb > file_size - 0x5A0:
                return None
            base = emb
        elif magic == b"\x7fCNT":
            base, is_debug, is_meta = 0, True, True
        else:
            return None
        f.seek(base)
        hdr = f.read(0x5A0)
        if len(hdr) < 0x5A0 or hdr[:4] != b"\x7fCNT":
            return None
        count = _u32be(hdr, 0x10)
        table_off = _u32be(hdr, 0x18)
        if count == 0 or count > 0x10000:
            return None
        f.seek(base + table_off)
        table = f.read(count * 0x20)
        if len(table) < count * 0x20:
            return None
        raw = []
        for i in range(count):
            o = i * 0x20
            raw.append((_u32be(table, o), _u32be(table, o + 4), _u32be(table, o + 8),
                        _u32be(table, o + 0x10), _u32be(table, o + 0x14)))
        names = {}
        for eid, name_off, flags, data_off, data_size in raw:
            if eid != 0x0200 or flags & 0x80000000 or not 0 < data_size <= 4 * 1024 * 1024:
                continue
            f.seek(base + data_off)
            nb = f.read(data_size)
            start = 0
            for i in range(len(nb) + 1):
                if i == len(nb) or nb[i] == 0:
                    if i > start:
                        names[start] = nb[start:i].decode("ascii", "replace")
                    start = i + 1
            break
        content_id = hdr[0x40:0x70].split(b"\x00")[0].decode("ascii", "replace").strip()
        entries = [(eid, names.get(name_off, ""), flags, data_off, data_size)
                   for eid, name_off, flags, data_off, data_size in raw]
        return cls(f, base, is_debug, is_meta, content_id, entries)

    def find(self, entry_id, name):
        for e in self.entries:
            if e[0] == entry_id and not e[2] & 0x80000000:
                return e
        for e in self.entries:
            if not e[2] & 0x80000000 and e[1].lower() == name.lower():
                return e
        return None

    def read_entry(self, e, cap):
        if e is None:
            return b""
        _eid, _name, flags, data_off, data_size = e
        if data_size == 0 or data_size > cap or flags & 0x80000000:
            return b""
        self.f.seek(self.base + data_off)
        data = self.f.read(data_size)
        return data if len(data) == data_size else b""

    def find_icon(self):
        def try_entry(e):
            b = self.read_entry(e, 8 * 1024 * 1024)
            return b if len(b) >= 8 and b[0] == 0x89 and b[1] == 0x50 else None

        for e in self.entries:
            if e[0] == 0x1200 and not e[2] & 0x80000000:
                hit = try_entry(e)
                if hit:
                    return hit
        for e in self.entries:
            if 0x1201 <= e[0] <= 0x1220 and not e[2] & 0x80000000:
                hit = try_entry(e)
                if hit:
                    return hit
        return None


def parse_sfo(b):
    """Minimal param.sfo parser (magic \\0PSF)."""
    if len(b) < 0x14 or b[:4] != b"\x00PSF":
        raise ValueError("not SFO")
    key_tab, data_tab, count = _u32le(b, 8), _u32le(b, 12), _u32le(b, 16)
    if count > 4096:
        raise ValueError("bad SFO count")
    out = {}
    for k in range(count):
        o = 0x14 + k * 0x10
        if o + 16 > len(b):
            raise ValueError("bad SFO entry")
        key_off = b[o] | (b[o + 1] << 8)
        fmt = b[o + 2] | (b[o + 3] << 8)
        length = _u32le(b, o + 4)
        data_off = _u32le(b, o + 12)
        start = key_tab + key_off
        end = b.find(b"\x00", start)
        name = b[start:end if end >= 0 else len(b)].decode("ascii", "replace")
        if fmt == 0x404:
            out[name] = "%08X" % _u32le(b, data_tab + data_off)
        elif fmt in (0x204, 0x4):
            slen = max(0, length - 1) if fmt == 0x204 else max(0, length)
            s = data_tab + data_off
            if s + slen > len(b):
                raise ValueError("bad SFO string")
            out[name] = b[s:s + slen].decode("utf-8", "replace").strip("\x00")
    return out


class PkgInfo:
    def __init__(self, **kw):
        self.path = kw.get("path", "")
        self.title = kw.get("title", "")
        self.content_id = kw.get("content_id", "")
        self.title_id = kw.get("title_id", "")
        self.version = kw.get("version", "")
        self.platform = kw.get("platform", "")
        self.category = kw.get("category", "")
        self.size = kw.get("size", 0)
        self.icon = kw.get("icon")
        self.params = kw.get("params", {})

    @property
    def role(self):
        if self.category.lower() == "ac":
            return "DLC"
        t = self.title.lower()
        c = self.content_id.lower()
        if any(m in t for m in ("dlc", "add-on", "addon", "expansion", "season pass")) \
                or "-dlc" in c or "_dlc" in c or "addon" in c:
            return "DLC"
        if self.category.lower() == "gp" or "patch" in t:
            return "Patch"
        return "Game"


def read_pkg(path):
    """Metadata of a .pkg (PS4 param.sfo first, PS5 param.json fallback)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            cnt = Cnt.open(f, size)
            if cnt is None:
                return None
            sfo_entry = cnt.find(0x1000, "param.sfo")
            if sfo_entry is not None:
                try:
                    sfo = parse_sfo(cnt.read_entry(sfo_entry, 1024 * 1024))
                except Exception:
                    sfo = None
                if sfo and (sfo.get("TITLE") or sfo.get("CONTENT_ID")):
                    return PkgInfo(
                        path=path, size=size,
                        title=sfo.get("TITLE", ""),
                        content_id=sfo.get("CONTENT_ID", ""),
                        title_id=sfo.get("TITLE_ID", ""),
                        category=sfo.get("CATEGORY", ""),
                        version=sfo.get("APP_VER") or sfo.get("VERSION", ""),
                        platform="PS4", icon=cnt.find_icon(),
                        params={k: v for k, v in sfo.items() if v})
            if not cnt.content_id:
                return None
            pj = cnt.find(0x2000, "param.json")
            if pj is None:
                return None
            title, version = "", ""
            try:
                j = json.loads(cnt.read_entry(pj, 2 * 1024 * 1024).decode("utf-8", "replace"))
                version = j.get("contentVersion", "") or ""
                lp = j.get("localizedParameters") or {}
                lang = lp.get("defaultLanguage") or "en-US"
                entry = lp.get(lang)
                if isinstance(entry, dict):
                    title = entry.get("titleName", "") or ""
                if not title:
                    for v in lp.values():
                        if isinstance(v, dict) and v.get("titleName"):
                            title = v["titleName"]
                            break
            except Exception:
                pass
            mid = cnt.content_id.split("-")[1] if "-" in cnt.content_id else cnt.content_id
            mid = mid.split("_")[0]
            title_id = mid if 4 <= len(mid) <= 16 else ""
            return PkgInfo(
                path=path, size=size, title=title or title_id or cnt.content_id,
                content_id=cnt.content_id, title_id=title_id, version=version,
                platform="PS5", icon=cnt.find_icon(),
                params={"CONTENT_ID": cnt.content_id, "TITLE_ID": title_id,
                        "VERSION": version, "PLATFORM": "PS5",
                        "BUILD": "Meta" if cnt.is_meta else
                                 ("Debug" if cnt.is_debug else "Retail")})
    except Exception:
        return None


# ------------------------------------------------------------ file server --


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # the receiver speaks 1.0 and closes

    def log_message(self, fmt, *args):
        if self.server.verbose:
            Out.info("  http %s - %s" % (self.client_address[0], fmt % args))

    def _not_found(self):
        self.send_response(404)
        self.send_header("Content-Length", "9")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"not found")

    def _serve_bytes(self, blob, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(blob)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle(self):
        target = urllib.parse.unquote(self.path.split("?", 1)[0])
        srv = self.server
        if target.startswith("/icon/"):
            item_id = target[len("/icon/"):]
            icon = srv.icons.get(item_id)
            if not icon or srv.is_revoked(item_id):
                return self._not_found()
            return self._serve_bytes(icon, "image/png")
        if target.startswith("/pkg/"):
            item_id = target[len("/pkg/"):]
        elif target == "/pkg":
            item_id = "pkg"
        else:
            return self._not_found()
        path = srv.files.get(item_id)
        if not path or not os.path.isfile(path) or srv.is_revoked(item_id):
            return self._not_found()
        srv.note_request(item_id)
        try:
            total = os.path.getsize(path)
        except OSError:
            return self._not_found()
        start, end, partial = 0, total - 1, False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and "," not in rng:
            spec = rng[6:].strip().split("-", 1)
            try:
                if spec[0] == "":
                    suffix = int(spec[1])
                    if suffix > 0:
                        start, partial = max(0, total - suffix), True
                else:
                    start = int(spec[0])
                    if start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % total)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if len(spec) > 1 and spec[1]:
                        end = min(int(spec[1]), total - 1)
                    partial = end >= start
            except ValueError:
                start, end, partial = 0, total - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(1024 * 1024, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
                    srv.add_served(item_id, len(buf))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    do_GET = _handle
    do_HEAD = _handle


class PkgServer(ThreadingHTTPServer):
    """Range-capable LAN server: /pkg/{id} payloads, /icon/{id} covers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, verbose=False):
        super().__init__(("0.0.0.0", port), _RangeHandler)
        self.files = {}
        self.icons = {}
        self.verbose = verbose
        self._served = {}
        self._revoked = set()
        self._requested = set()
        self._lock = threading.Lock()

    def register(self, item_id, path, icon=None):
        self.files[item_id] = path
        if icon:
            self.icons[item_id] = icon
        with self._lock:
            self._served[item_id] = 0
            self._revoked.discard(item_id)
            self._requested.discard(item_id)

    def add_served(self, item_id, n):
        with self._lock:
            self._served[item_id] = self._served.get(item_id, 0) + n

    def served(self, item_id):
        with self._lock:
            return self._served.get(item_id, 0)

    def note_request(self, item_id):
        with self._lock:
            self._requested.add(item_id)

    def was_requested(self, item_id):
        with self._lock:
            return item_id in self._requested

    def revoke(self, item_id):
        with self._lock:
            self._revoked.add(item_id)

    def is_revoked(self, item_id):
        with self._lock:
            return item_id in self._revoked

    def url_for(self, host, item_id):
        return "http://%s:%d/pkg/%s" % (host, self.server_address[1],
                                        urllib.parse.quote(item_id))

    def icon_url_for(self, host, item_id):
        return "http://%s:%d/icon/%s" % (host, self.server_address[1],
                                         urllib.parse.quote(item_id))

    def start(self):
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True).start()


def start_server(port, verbose=False):
    try:
        srv = PkgServer(port, verbose)
    except OSError as ex:
        raise SystemExit("error: cannot bind port %d (%s) - try --port" % (port, ex))
    srv.start()
    return srv


# ----------------------------------------------------------- ps resolution --


def resolve_console(args):
    """--ps > $PKGSENDER_PS > config > auto-discover (then remembered)."""
    ip = getattr(args, "ps", None) or os.environ.get("PKGSENDER_PS")
    cfg = load_config()
    if not ip:
        ip = cfg.get("ps_ip")
    if ip:
        return ip
    Out.info("No console configured - discovering...")
    found = [c for c in find_consoles() if c.get("online")]
    if not found:
        raise SystemExit("error: no console found. Start pkg-receiver.elf on the "
                         "console, or pass --ps <ip>.")
    if len(found) > 1:
        Out.warn("several consoles found: %s - using %s (pass --ps to choose)" %
                 (", ".join(c["ip"] for c in found), found[0]["ip"]))
    ip = found[0]["ip"]
    cfg["ps_ip"] = ip
    save_config(cfg)
    Out.info("Using console %s (remembered in %s)" % (ip, config_path()))
    return ip


def resolve_pc_ip(args, ps_ip):
    ip = getattr(args, "pc", None) or os.environ.get("PKGSENDER_PC") or load_config().get("pc_ip")
    if ip:
        return ip
    ip = local_ip_for(ps_ip)
    if not ip:
        raise SystemExit("error: cannot determine this machine's LAN address - "
                         "pass --pc <ip>.")
    return ip


def expand_pkgs(paths, recursive=True):
    """Files as given; directories contribute their .pkg files."""
    out = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            hits = []
            for root, _dirs, files in os.walk(p):
                for name in files:
                    if name.lower().endswith(".pkg"):
                        hits.append(os.path.join(root, name))
                if not recursive:
                    break
            if not hits:
                Out.warn("no .pkg files in %s" % p)
            out.extend(sorted(hits))
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise SystemExit("error: no such file or directory: %s" % p)
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# ---------------------------------------------------------------- commands --


def cmd_discover(args):
    consoles = find_consoles(beacon_seconds=args.beacon, do_sweep=not args.no_sweep)
    if args.json:
        print(json.dumps(consoles, indent=2))
    else:
        Out.endline()
        if not consoles:
            print("No console found.")
        else:
            print("%-16s %-18s %s" % ("ADDRESS", "SOURCE", "STATE"))
            for c in consoles:
                state = "not a receiver" if not c.get("online") else (
                    "busy" if c.get("busy") else
                    ("idle" if c.get("busy") is False else "online"))
                print("%-16s %-18s %s" % (c["ip"], c["source"], state))
    online = [c for c in consoles if c.get("online")]
    if online and args.save:
        cfg = load_config()
        cfg["ps_ip"] = online[0]["ip"]
        save_config(cfg)
        Out.info("Saved %s as the default console." % online[0]["ip"])
    return 0 if online else 1


def cmd_status(args):
    ps = resolve_console(args)
    rec = Receiver(ps)
    online = rec.is_online()
    supported, st = rec.status()
    build = rec.version()
    info = {"ip": ps, "port": rec.port, "online": online, "build": build,
            "status_supported": supported, "status": st,
            "pc_ip": local_ip_for(ps)}
    if args.json:
        print(json.dumps(info, indent=2))
        return 0 if online else 1
    Out.endline()
    print("Console:     %s:%d" % (ps, rec.port))
    print("Receiver:    %s" % ("online" if online else "NOT RESPONDING"))
    if build:
        print("Build:       %s" % build)
    if supported:
        print("Installs:    %s (%s active)" % (
            "busy" if st.get("busy") else "idle", st.get("active", 0)))
        if st.get("pull"):
            got, want = int(st.get("pullGot", 0)), int(st.get("pullWant", -1))
            print("Pull copy:   %s %s / %s%s" % (
                st.get("pullName", ""), fmt_size(got), fmt_size(want),
                " (paused)" if st.get("pullPaused") else ""))
    else:
        print("Installs:    unknown (receiver has no /api/status)")
    print("This PC:     %s" % (info["pc_ip"] or "unknown"))
    return 0 if online else 1


def cmd_info(args):
    rc = 0
    for path in expand_pkgs(args.files):
        info = read_pkg(path)
        if info is None:
            Out.error("cannot parse %s" % path)
            rc = 1
            continue
        if args.json:
            print(json.dumps({"path": path, "title": info.title,
                              "contentId": info.content_id, "titleId": info.title_id,
                              "version": info.version, "platform": info.platform,
                              "role": info.role, "size": info.size,
                              "hasIcon": bool(info.icon), "params": info.params},
                             indent=2))
            continue
        print("%s" % path)
        print("  Title      %s" % (info.title or "-"))
        print("  Title ID   %s" % (info.title_id or "-"))
        print("  Content ID %s" % (info.content_id or "-"))
        print("  Version    %s" % (info.version or "-"))
        print("  Platform   %s (%s)" % (info.platform or "?", info.role))
        print("  Size       %s (%d bytes)" % (fmt_size(info.size), info.size))
        print("  Cover      %s" % ("yes, %s" % fmt_size(len(info.icon))
                                   if info.icon else "no"))
    return rc


def _wait_download(srv, item_id, size, label, stop_after_idle, pc_ip):
    """Block until the console has pulled the whole file. True when done."""
    prog = Progress(label, size)
    t0 = time.monotonic()
    last_served, last_change = 0, t0
    while True:
        served = min(srv.served(item_id), size)
        started = srv.was_requested(item_id)
        if served > last_served:
            last_served, last_change = served, time.monotonic()
        if started:
            prog.update(served)
        else:
            prog.update(0, note="queued on console...", force=True)
        if served >= size:
            prog.update(size, force=True)
            prog.finish("downloaded")
            return True
        idle = time.monotonic() - (last_change if started else t0)
        if idle > stop_after_idle:
            Out.endline()
            if not started:
                Out.error("%s: the console never asked for the file (%ds). "
                          "Check that it can reach http://%s:%d (LAN, firewall, "
                          "--pc address)."
                          % (label, int(stop_after_idle), pc_ip,
                             srv.server_address[1]))
            else:
                Out.error("%s: download stalled at %s / %s for %ds."
                          % (label, fmt_size(served), fmt_size(size),
                             int(stop_after_idle)))
            return False
        time.sleep(0.25)


def _wait_install(rec, label, timeout_idle=300, timeout_total=3 * 3600):
    """After the download: busy -> idle means the install finished."""
    t0 = time.monotonic()
    was_busy = False
    while True:
        supported, st = rec.status()
        if not supported:
            Out.info("  %s: receiver has no /api/status - install not tracked" % label)
            return None
        if st.get("busy"):
            was_busy = True
            Out.status("%s installing on console..." % label)
        elif was_busy:
            Out.endline()
            return True
        elif time.monotonic() - t0 > timeout_idle:
            Out.endline()
            Out.info("  %s: console never reported an install - moving on" % label)
            return None
        if time.monotonic() - t0 > timeout_total:
            Out.endline()
            Out.warn("%s: install still running after 3h - moving on" % label)
            return None
        time.sleep(2)


def cmd_send(args):
    ps = resolve_console(args)
    files = expand_pkgs(args.files)
    if not files:
        raise SystemExit("error: nothing to send")
    rec = Receiver(ps)
    if not args.force and not rec.is_online():
        raise SystemExit("error: no receiver at %s (ports %s). Start "
                         "pkg-receiver.elf, or pass --force to try anyway."
                         % (ps, "/".join(str(p) for p in RECEIVER_PORTS)))
    pc = resolve_pc_ip(args, ps)
    srv = start_server(args.port, args.verbose)
    Out.info("Serving from http://%s:%d  ->  console %s:%d"
             % (pc, srv.server_address[1], ps, rec.port))

    session = "%04x" % (int(time.time()) & 0xFFFF)
    items = []
    for i, path in enumerate(files, 1):
        size = os.path.getsize(path)
        info = None if args.no_parse else read_pkg(path)
        items.append({"path": path, "size": size, "id": "%s-%d" % (session, i),
                      "title": (info.title if info and info.title
                                else os.path.basename(path)),
                      "icon": info.icon if info else None})
    total = sum(it["size"] for it in items)
    Out.info("Queue: %d package(s), %s" % (len(items), fmt_size(total)))

    failed = 0
    try:
        for n, it in enumerate(items, 1):
            label = "[%d/%d] %s" % (n, len(items), it["title"])
            srv.register(it["id"], it["path"], it["icon"])
            url = srv.url_for(pc, it["id"])
            icon_url = srv.icon_url_for(pc, it["id"]) if it["icon"] else None
            if args.dry_run:
                Out.info("%s would install %s (%s)" % (label, url, fmt_size(it["size"])))
                continue
            try:
                ok, reply = rec.install(url, it["title"], icon_url)
            except ReceiverError as ex:
                ok, reply = False, str(ex)
            if not ok:
                Out.error("%s push rejected: %s" % (label, reply))
                failed += 1
                continue
            if not _wait_download(srv, it["id"], it["size"], label,
                                  args.pull_timeout, pc):
                srv.revoke(it["id"])
                failed += 1
                continue
            if not args.no_wait:
                _wait_install(rec, label)
    except KeyboardInterrupt:
        Out.endline()
        Out.warn("interrupted - revoking pending downloads")
        for it in items:
            srv.revoke(it["id"])
        return 130
    finally:
        srv.shutdown()

    if args.dry_run:
        return 0
    sent = len(items) - failed
    Out.info("Finished: %d sent, %d failed." % (sent, failed))
    return 0 if failed == 0 else 1


def _copy_targets(paths, remote_dir):
    """[(local, remote)] - files as-is, folders keep their structure."""
    jobs = []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        base = remote_dir.rstrip("/")
        if os.path.isdir(p):
            root_name = os.path.basename(p.rstrip("/"))
            for root, _dirs, files in os.walk(p):
                for name in sorted(files):
                    local = os.path.join(root, name)
                    rel = os.path.relpath(local, p).replace(os.sep, "/")
                    jobs.append((local, "%s/%s/%s" % (base, root_name, rel)))
        elif os.path.isfile(p):
            jobs.append((p, "%s/%s" % (base, os.path.basename(p))))
        else:
            raise SystemExit("error: no such file or directory: %s" % p)
    return jobs


def _copy_one(rec, local, remote, size, chunk_size, label, force, retries=3):
    for attempt in range(1, retries + 1):
        try:
            offset = 0
            try:
                exists, remote_size = rec.stat(remote)
            except ReceiverError as ex:
                Out.warn("%s: stat failed (%s) - starting from 0" % (label, ex))
                exists, remote_size = False, 0
            if exists:
                if not force and remote_size == size and size > 0:
                    Out.info("%s  already on console (%s) - skipped"
                             % (label, fmt_size(size)))
                    return True
                if 0 < remote_size < size:
                    offset = remote_size
            prog = Progress(label, size)
            with open(local, "rb") as f:
                f.seek(offset)
                sent = offset
                prog.update(sent, force=True)
                while sent < size:
                    buf = f.read(min(chunk_size, size - sent))
                    if not buf:
                        break
                    rec.write(remote, sent, buf)
                    sent += len(buf)
                    prog.update(sent)
            rec.done(remote, size)
            exists, remote_size = rec.stat(remote)
            if not exists or remote_size != size:
                raise ReceiverError("verify failed: console has %s, expected %s"
                                    % (fmt_size(remote_size), fmt_size(size)))
            prog.update(size, force=True)
            prog.finish("resumed + done" if offset else "done")
            return True
        except KeyboardInterrupt:
            raise
        except (ReceiverError, OSError) as ex:
            Out.endline()
            if attempt >= retries:
                Out.error("%s: %s" % (label, ex))
                return False
            Out.warn("%s: %s - retry %d/%d" % (label, ex, attempt, retries))
            time.sleep(attempt)
    return False


def cmd_copy(args):
    # cp-style: `copy a.pkg /data/homebrew` - a trailing absolute path that is
    # not a local file/folder is the destination, not something to send.
    files = list(args.files)
    dest = args.dest
    if len(files) > 1 and files[-1].startswith("/") and \
            not os.path.exists(os.path.expanduser(files[-1])):
        dest = files.pop()
        Out.info("Destination on the console: %s" % dest)
    ps = resolve_console(args)
    rec = Receiver(ps)
    if not args.force_offline and not rec.is_online():
        raise SystemExit("error: no receiver at %s - start pkg-receiver.elf." % ps)
    chunk = max(MIN_CHUNK, min(args.chunk, MAX_CHUNK))
    if chunk != args.chunk:
        Out.warn("chunk clamped to %s (receiver limit)" % fmt_size(chunk))
    jobs = _copy_targets(files, dest)
    if not jobs:
        raise SystemExit("error: nothing to copy")
    total = sum(os.path.getsize(j[0]) for j in jobs)
    Out.info("Copying %d file(s), %s -> %s:%s"
             % (len(jobs), fmt_size(total), ps, dest))
    made = set()
    failed = 0
    try:
        for n, (local, remote) in enumerate(jobs, 1):
            size = os.path.getsize(local)
            label = "[%d/%d] %s" % (n, len(jobs), os.path.basename(local))
            if args.dry_run:
                Out.info("%s would write %s (%s)" % (label, remote, fmt_size(size)))
                continue
            parent = remote.rsplit("/", 1)[0]
            if parent and parent not in made:
                made.add(parent)
                try:
                    rec.mkdir(parent)
                except ReceiverError as ex:
                    Out.warn("mkdir %s: %s" % (parent, ex))
            if not _copy_one(rec, local, remote, size, chunk, label, args.force):
                failed += 1
    except KeyboardInterrupt:
        Out.endline()
        Out.warn("interrupted - partial files stay on the console for resume")
        return 130
    if args.dry_run:
        return 0
    Out.info("Finished: %d copied, %d failed." % (len(jobs) - failed, failed))
    return 0 if failed == 0 else 1


def cmd_serve(args):
    ps = getattr(args, "ps", None) or load_config().get("ps_ip")
    pc = resolve_pc_ip(args, ps)
    files = expand_pkgs(args.files)
    if not files:
        raise SystemExit("error: nothing to serve")
    srv = start_server(args.port, args.verbose)
    Out.endline()
    print("Serving %d file(s) on http://%s:%d - Ctrl-C to stop\n"
          % (len(files), pc, srv.server_address[1]))
    for i, path in enumerate(files, 1):
        item_id = "f%d" % i
        info = None if args.no_parse else read_pkg(path)
        srv.register(item_id, path, info.icon if info else None)
        print("  %-42s %10s  %s" % (
            (info.title if info and info.title else os.path.basename(path))[:42],
            fmt_size(os.path.getsize(path)), srv.url_for(pc, item_id)))
    sys.stdout.flush()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        Out.info("\nStopped.")
        srv.shutdown()
    return 0


def cmd_config(args):
    cfg = load_config()
    changed = False
    if args.ps_ip:
        cfg["ps_ip"] = args.ps_ip
        changed = True
    if args.pc_ip:
        cfg["pc_ip"] = args.pc_ip
        changed = True
    if args.clear:
        cfg = {}
        changed = True
    if changed:
        save_config(cfg)
    Out.endline()
    print("config file: %s" % config_path())
    for key in ("ps_ip", "pc_ip"):
        print("  %-7s %s" % (key, cfg.get(key, "(unset)")))
    return 0


# ------------------------------------------------------------------- main --


def build_parser():
    p = argparse.ArgumentParser(
        prog="pkgsender",
        description="Send PKGs to a jailbroken PS5/PS4 from Linux or macOS.",
        epilog="The console pulls the package from this machine, so keep "
               "pkgsender running until the transfer finishes.")
    p.add_argument("--version", action="version", version="pkgsender " + VERSION)
    p.add_argument("-q", "--quiet", action="store_true", help="only errors")
    p.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")
    sub = p.add_subparsers(dest="command", required=True)

    def with_console(sp):
        sp.add_argument("--ps", metavar="IP", help="console address (default: "
                                                   "config, else auto-discover)")
        return sp

    sp = sub.add_parser("discover", help="find consoles on the LAN")
    sp.add_argument("--beacon", type=float, default=3.0, metavar="SEC",
                    help="seconds to listen for receiver beacons (default 3)")
    sp.add_argument("--no-sweep", action="store_true",
                    help="beacon only, never sweep the subnet")
    sp.add_argument("--save", action="store_true",
                    help="remember the first console found as the default")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_discover)

    sp = with_console(sub.add_parser("status", help="receiver state and build"))
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_status)

    sp = with_console(sub.add_parser(
        "send", help="install PKGs on the console (serve + push + track)"))
    sp.add_argument("files", nargs="+", metavar="PKG",
                    help=".pkg files, or folders to take .pkg files from")
    sp.add_argument("--pc", metavar="IP",
                    help="address the console should pull from (default: auto)")
    sp.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT,
                    help="local file-server port (default %d)" % DEFAULT_SERVE_PORT)
    sp.add_argument("--no-wait", action="store_true",
                    help="stop tracking once the download completes")
    sp.add_argument("--no-parse", action="store_true",
                    help="skip PKG metadata/cover parsing (send file names)")
    sp.add_argument("--pull-timeout", type=int, default=900, metavar="SEC",
                    help="give up if the console never starts downloading "
                         "(default 900)")
    sp.add_argument("--force", action="store_true",
                    help="push even if the receiver probe fails")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, push nothing")
    sp.set_defaults(func=cmd_send)

    sp = with_console(sub.add_parser(
        "copy", help="copy files to the console filesystem (e.g. the payload)"))
    sp.add_argument("files", nargs="+", metavar="PATH",
                    help="files or folders to copy; a trailing path that does "
                         "not exist locally is taken as the remote directory")
    sp.add_argument("-d", "--dest", default=DEFAULT_REMOTE_DIR, metavar="DIR",
                    help="remote directory (default %s)" % DEFAULT_REMOTE_DIR)
    sp.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, metavar="BYTES",
                    help="upload chunk size (default %d, max %d)"
                         % (DEFAULT_CHUNK, MAX_CHUNK))
    sp.add_argument("--force", action="store_true",
                    help="re-send even if the console already has the same size")
    sp.add_argument("--force-offline", action="store_true",
                    help="copy even if the receiver probe fails")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be copied, write nothing")
    sp.set_defaults(func=cmd_copy)

    sp = with_console(sub.add_parser(
        "serve", help="just serve PKGs over HTTP and print their URLs"))
    sp.add_argument("files", nargs="+", metavar="PKG")
    sp.add_argument("--pc", metavar="IP", help="address to advertise (default: auto)")
    sp.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT,
                    help="local file-server port (default %d)" % DEFAULT_SERVE_PORT)
    sp.add_argument("--no-parse", action="store_true", help="skip PKG parsing")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("info", help="read PKG metadata (title, id, version)")
    sp.add_argument("files", nargs="+", metavar="PKG")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("config", help="show or change saved defaults")
    sp.add_argument("--ps-ip", metavar="IP", help="default console address")
    sp.add_argument("--pc-ip", metavar="IP", help="default PC address")
    sp.add_argument("--clear", action="store_true", help="forget everything")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    Out.quiet = args.quiet
    try:
        return args.func(args)
    except KeyboardInterrupt:
        Out.endline()
        Out.warn("interrupted")
        return 130
    except ReceiverError as ex:
        Out.error(str(ex))
        return 1


if __name__ == "__main__":
    sys.exit(main())
