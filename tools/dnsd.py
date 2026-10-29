#!/usr/bin/env python3
"""A small authoritative-ish DNS upstream for the stand.

UDP and TCP on the same port, stdlib only. It answers from a JSON zone so the
bench controls every name the engines resolve:

  {"ttl": 60, "names": {"n000.dns.test": {"A": ["203.0.113.7"],
                                          "AAAA": ["::1"]}}}

A/AAAA are answered from the zone; PTR is answered from the reverse of the
same records; HTTPS and every other type get an empty NOERROR (NODATA). An
unknown name gets NXDOMAIN. That is what a normal resolver expects, unlike a
stub that only understands A.

Modes:
  --delay-ms N   sleep N ms before every answer
  --fail         never answer (a black hole that still logs the query)
  --refuse       answer REFUSED to every query

  --log FILE     JSONL, one line per received query:
                 {"ts": ..., "transport": "udp"|"tcp", "name": ..., "qtype": ..., "id": ...}
  --stats [FILE] counts per name/qtype, dumped on SIGTERM (and SIGINT);
                 omitted value means stderr. With --log the dump also goes to
                 <log>.stats.json when no explicit FILE is given.
"""
import argparse
import ipaddress
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import Counter

QTYPES = {
    1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT",
    28: "AAAA", 33: "SRV", 35: "NAPTR", 41: "OPT", 43: "DS", 46: "RRSIG",
    47: "NSEC", 48: "DNSKEY", 64: "SVCB", 65: "HTTPS", 255: "ANY",
}
QTYPE_NUM = {v: k for k, v in QTYPES.items()}


def qtype_name(n):
    return QTYPES.get(n, f"TYPE{n}")


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.by_transport = Counter()
        self.by_name = {}
        self.by_qtype = Counter()

    def count(self, name, qtype, transport):
        with self.lock:
            self.total += 1
            self.by_transport[transport] += 1
            self.by_qtype[qtype] += 1
            self.by_name.setdefault(name, Counter())[qtype] += 1

    def dump(self):
        with self.lock:
            names = [
                {"name": n, "qtype": t, "count": c}
                for n, counts in sorted(self.by_name.items())
                for t, c in sorted(counts.items())
            ]
            return {
                "total": self.total,
                "by_transport": dict(self.by_transport),
                "by_qtype": dict(self.by_qtype),
                "names": names,
            }


def parse_query(data):
    """Return (id, flags, qname, qtype, qclass, question_bytes) or None."""
    if len(data) < 12:
        return None
    qid, flags, qdcount = struct.unpack("!HHH", data[:6])
    if qdcount < 1:
        return None
    off = 12
    labels = []
    while True:
        if off >= len(data):
            return None
        length = data[off]
        off += 1
        if length == 0:
            break
        if length & 0xC0:
            return None  # compression in a question: not something we send
        if off + length > len(data):
            return None
        labels.append(data[off:off + length].decode("ascii", "replace"))
        off += length
    if off + 4 > len(data):
        return None
    qtype, qclass = struct.unpack("!HH", data[off:off + 4])
    question = data[12:off + 4]
    return qid, flags, ".".join(labels).lower(), qtype, qclass, question


def enc_name(name):
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        raw = label.encode("ascii")
        if len(raw) > 63:
            raise ValueError("label too long")
        out.append(len(raw))
        out += raw
    out.append(0)
    return bytes(out)


def rr(name_ptr, rtype, ttl, rdata):
    return name_ptr + struct.pack("!HHIH", rtype, 1, ttl, len(rdata)) + rdata


class Zone:
    def __init__(self, path):
        raw = json.load(open(path))
        self.ttl = int(raw.get("ttl", 60))
        self.names = {}
        for name, records in raw.get("names", {}).items():
            entry = {}
            for rtype, values in records.items():
                rtype = rtype.upper()
                if rtype == "A":
                    entry["A"] = [ipaddress.IPv4Address(v).packed for v in values]
                elif rtype == "AAAA":
                    entry["AAAA"] = [ipaddress.IPv6Address(v).packed for v in values]
            self.names[name.rstrip(".").lower()] = entry
        self.ptr = {}
        # reverse pointers; the first name in sorted order wins for shared IPs
        for name in sorted(self.names):
            for packed in self.names[name].get("A", []):
                self.ptr.setdefault(ipaddress.IPv4Address(packed).reverse_pointer, name)
            for packed in self.names[name].get("AAAA", []):
                self.ptr.setdefault(ipaddress.IPv6Address(packed).reverse_pointer, name)

    def answer(self, qname, qtype):
        """Return (rcode, [(rtype, rdata)]) for a question."""
        key = qname.rstrip(".").lower()
        if qtype_name(qtype) == "PTR":
            name = self.ptr.get(key)
            if name is None:
                return 3, []  # NXDOMAIN
            return 0, [(12, enc_name(name))]
        entry = self.names.get(key)
        if entry is None:
            return 3, []  # NXDOMAIN
        if qtype == 1:
            return 0, [(1, r) for r in entry.get("A", [])]
        if qtype == 28:
            return 0, [(28, r) for r in entry.get("AAAA", [])]
        # NODATA: the name exists, this type does not (HTTPS, TXT, ...)
        return 0, []


class Server:
    def __init__(self, args, zone):
        self.args = args
        self.zone = zone
        self.stats = Stats()
        self.log_lock = threading.Lock()
        self.log = open(args.log, "a", buffering=1) if args.log else None
        self.running = True

    def record(self, transport, payload, addr):
        parsed = parse_query(payload)
        if parsed is None:
            return None
        qid, flags, qname, qtype, qclass, question = parsed
        name = qname
        tname = qtype_name(qtype)
        self.stats.count(name, tname, transport)
        if self.log:
            line = json.dumps({
                "ts": time.time(),
                "transport": transport,
                "name": name,
                "qtype": tname,
                "id": qid,
                "from": f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr),
            }, separators=(",", ":"))
            with self.log_lock:
                self.log.write(line + "\n")
        return parsed

    def response(self, parsed):
        qid, flags, qname, qtype, qclass, question = parsed
        if self.args.refuse:
            rcode, answers = 5, []
        else:
            rcode, answers = self.zone.answer(qname, qtype)
        # QR, copy opcode+RD, AA, RA
        out_flags = 0x8000 | (flags & 0x7900) | 0x0400 | 0x0080 | rcode
        header = struct.pack("!HHHHHH", qid, out_flags, 1, len(answers), 0, 0)
        body = bytearray(header)
        body += question
        name_ptr = b"\xc0\x0c"
        for rtype, rdata in answers:
            body += rr(name_ptr, rtype, self.zone.ttl, rdata)
        return bytes(body)

    def maybe_delay(self):
        if self.args.delay_ms:
            time.sleep(self.args.delay_ms / 1000.0)

    def udp_loop(self, sock):
        while self.running:
            try:
                payload, addr = sock.recvfrom(4096)
            except OSError:
                return
            threading.Thread(target=self.udp_one, args=(sock, payload, addr), daemon=True).start()

    def udp_one(self, sock, payload, addr):
        parsed = self.record("udp", payload, addr)
        if parsed is None or self.args.fail:
            return
        self.maybe_delay()
        try:
            sock.sendto(self.response(parsed), addr)
        except OSError:
            pass

    def tcp_loop(self, listener):
        while self.running:
            try:
                conn, addr = listener.accept()
            except OSError:
                return
            threading.Thread(target=self.tcp_conn, args=(conn, addr), daemon=True).start()

    def tcp_conn(self, conn, addr):
        try:
            conn.settimeout(30)
            while self.running:
                head = recv_exact(conn, 2)
                if head is None:
                    return
                (length,) = struct.unpack("!H", head)
                payload = recv_exact(conn, length)
                if payload is None:
                    return
                parsed = self.record("tcp", payload, addr)
                if parsed is None or self.args.fail:
                    # fail: hold the connection open, never answer
                    time.sleep(5)
                    return
                self.maybe_delay()
                out = self.response(parsed)
                conn.sendall(struct.pack("!H", len(out)) + out)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def dump_stats(self):
        data = self.stats.dump()
        text = json.dumps(data, indent=2, sort_keys=True)
        target = self.args.stats
        if target is True:
            target = (self.args.log + ".stats.json") if self.args.log else "-"
        if target and target != "-":
            try:
                with open(target + ".tmp", "w") as f:
                    f.write(text + "\n")
                os.replace(target + ".tmp", target)
            except OSError as exc:
                print(f"dnsd: cannot write stats: {exc}", file=sys.stderr, flush=True)
        if target == "-" or not self.args.log:
            print(text, file=sys.stderr, flush=True)


def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5353)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--zone", default=str(os.path.join(os.path.dirname(__file__), "dns-zone.json")))
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--fail", action="store_true", help="never answer")
    ap.add_argument("--refuse", action="store_true", help="answer REFUSED")
    ap.add_argument("--log", help="JSONL of received queries")
    ap.add_argument("--stats", nargs="?", const=True, default=None,
                    help="dump name/qtype counts on SIGTERM (default: <log>.stats.json)")
    args = ap.parse_args()

    zone = Zone(args.zone)
    server = Server(args, zone)

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind((args.bind, args.port))
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind((args.bind, args.port))
    tcp.listen(128)

    def stop(signum, frame):
        server.running = False
        server.dump_stats()
        try:
            udp.close()
        except OSError:
            pass
        try:
            tcp.close()
        except OSError:
            pass
        # daemon worker threads die with the process
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threading.Thread(target=server.udp_loop, args=(udp,), daemon=True).start()
    threading.Thread(target=server.tcp_loop, args=(tcp,), daemon=True).start()
    print(f"dnsd: {len(zone.names)} names on {args.bind}:{args.port} "
          f"(udp+tcp, delay={args.delay_ms}ms"
          f"{', fail' if args.fail else ''}{', refuse' if args.refuse else ''})",
          file=sys.stderr, flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
