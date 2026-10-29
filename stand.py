#!/usr/bin/env python3
"""Reproducible performance stand for proxy engines.

Subcommands:
  up <scenario>                 start the scenario's server and client
  down [<scenario>|--all]       stop processes of one scenario (or all)
  run <scenario> <rtt_ms>       measure; --quick, --runs N, --label TEXT
  ab <a> <b> <rtt_ms>           alternate two scenarios; --rounds N
  report [<glob>]               markdown table from results/*.json
  check <scenario>              negative controls: the tunnel must be used
  dns <scenario> <sub> [N]      DNS scenarios through the stand: cold, warm,
                                stampede, dead, leak (see tools/dnsd.py)

Everything the result depends on is recorded in the result file: binary
hashes, versions, kernel, sysctls, netem parameters, git commit.
"""
import argparse
import copy
import glob
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BENCH = ROOT / "bench/target/release/bench"
STATE = ROOT / ".state"
PIDS = STATE / "pids"
RESULTS = ROOT / "results"

SCENARIOS = json.load(open(ROOT / "lib/scenarios.json"))


def env(name, default=None):
    # The shell wins over .state/bins.env, so a single run can point at another build.
    if name in os.environ:
        return os.environ[name]
    bins = ROOT / ".state/bins.env"
    if bins.exists():
        for line in bins.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k == name:
                    return v
    return default


VERSIONS = {}
for line in (ROOT / "versions.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        VERSIONS[k] = v

BINS = {
    "xray": ROOT / "cache/xray/xray",
    "singbox": ROOT / "cache/sing-box/sing-box",
    "unbihex-server": lambda: env("UNBIHEX_SERVER", ""),
    "unbihex-cli": lambda: env("UNBIHEX_CLI", ""),
}


def bin_path(name):
    b = BINS[name]
    p = Path(b() if callable(b) else b)
    if not p.exists():
        sys.exit(f"missing binary {name}: {p} (run setup.sh, or set UNBIHEX_REPO)")
    return p


def say(msg):
    print(msg, flush=True)


def sh(cmd, check=False):
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)


def socks_port(scenario):
    return SCENARIOS[scenario]["socks"]


def tunnel_ports():
    return sorted({p for s in SCENARIOS.values() for p in s["ports"]})


def set_rtt(ms):
    """Delay only the tunnel ports on lo, both directions, so the sink and the
    SOCKS handshake are not delayed (that would distort TTFB)."""
    sh("tc qdisc del dev lo root 2>/dev/null")
    if ms <= 0:
        return
    half = ms // 2
    sh("tc qdisc add dev lo root handle 1: prio bands 4 priomap " + " ".join(["0"] * 16))
    sh(f"tc qdisc add dev lo parent 1:2 handle 20: netem delay {half}ms limit 20000")
    sh(f"tc qdisc add dev lo parent 1:3 handle 30: netem delay {half}ms limit 20000")
    for p in tunnel_ports():
        sh(f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport {p} 0xffff flowid 1:2")
        sh(f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip sport {p} 0xffff flowid 1:3")


def pid_file(scenario, name):
    return PIDS / f"{scenario}-{name}.pid"


def stop_all():
    """One scenario at a time: orphan servers would share ports (Xray uses
    SO_REUSEPORT) and the kernel would silently balance connections between them."""
    # Only scenario processes: dest and sink are owned by setup.sh and must survive.
    for f in list(PIDS.glob("*-server.pid")) + list(PIDS.glob("*-client.pid")):
        try:
            pid = int(f.read_text())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        f.unlink(missing_ok=True)


def stop(scenario):
    for f in PIDS.glob(f"{scenario}-*.pid"):
        try:
            pid = int(f.read_text())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        f.unlink(missing_ok=True)


def wait_port(port, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if sh(f"ss -lnt | grep -q ':{port} '").returncode == 0:
            return True
        time.sleep(0.1)
    return False


def infra_up():
    """dest and the sink are started by setup.sh; a scenario without them fails obscurely."""
    dest = VERSIONS["DEST_PORT"]
    sink = f"{VERSIONS['SINK_IP']}:{VERSIONS['SINK_PORT']}"
    ok = sh(f"ss -lnt | grep -q ':{dest} '").returncode == 0
    ok = ok and sh(f"ss -lnt | grep -q '{sink}'").returncode == 0
    return ok


def up(scenario):
    if scenario not in SCENARIOS:
        sys.exit(f"unknown scenario {scenario}; known: {', '.join(SCENARIOS)}")
    if not infra_up():
        say("dest or sink is not running; calling setup.sh")
        subprocess.run([str(ROOT / "setup.sh")], cwd=ROOT, check=True)
    stop_all()
    for proc in SCENARIOS[scenario]["start"]:
        cmd = [str(bin_path(proc["bin"]))] + proc["args"]
        log = open(ROOT / f"logs/{scenario}-{proc['name']}.log", "w")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        pid_file(scenario, proc["name"]).write_text(str(p.pid))
    port = socks_port(scenario)
    if not wait_port(port):
        for name in ("server", "client"):
            log = ROOT / f"logs/{scenario}-{name}.log"
            if log.exists():
                say(f"--- {log.name} ---\n" + log.read_text()[-2000:])
        sys.exit(f"scenario {scenario}: SOCKS port {port} did not come up")
    # The port must belong to the client we just started, not to a leftover.
    listener = sh(f"ss -lntp | grep ':{port} '").stdout
    client_pid = pid_file(scenario, "client").read_text()
    if f"pid={client_pid}" not in listener:
        sys.exit(f"scenario {scenario}: port {port} is not owned by pid {client_pid}:\n{listener}")
    say(f"up: {scenario} (socks 127.0.0.1:{port})")


def cpu_ticks(pids):
    total = 0
    for pid in pids:
        try:
            stat = open(f"/proc/{pid}/stat").read()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        total += int(fields[11]) + int(fields[12])
    return total


def repo_of(path):
    """The git repository a binary was built in (worktrees included)."""
    p = Path(path).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def git_info(repo):
    return {
        "commit": sh(f"git -C {repo} rev-parse HEAD").stdout.strip(),
        "branch": sh(f"git -C {repo} rev-parse --abbrev-ref HEAD").stdout.strip(),
        "dirty": sh(f"git -C {repo} status --porcelain").stdout.strip() != "",
    }


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_of(path):
    for args in (["version"], ["--version"]):
        r = subprocess.run([str(path)] + args, capture_output=True, text=True, timeout=10)
        line = (r.stdout or r.stderr).strip().splitlines()
        if line:
            return line[0][:120]
    return "?"


def provenance(scenario, rtt):
    pids = {}
    for role in ("server", "client"):
        f = pid_file(scenario, role)
        pids[role] = int(f.read_text()) if f.exists() else None
    info = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario": scenario,
        "rtt_ms": rtt,
        "kernel": platform.release(),
        "cpu": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo")
                     if l.startswith("model name")), "?"),
        "nproc": os.cpu_count(),
        "sysctl": {k: sh(f"sysctl -n {k}").stdout.strip()
                   for k in ("net.ipv4.tcp_rmem", "net.core.rmem_max", "net.core.wmem_max")},
        "netem": {"one_way_ms": rtt // 2, "limit": 20000, "ports": tunnel_ports()},
        "scenario_procs": pids,
        "inputs": {},
    }
    for name in ("xray", "singbox", "unbihex-server", "unbihex-cli"):
        try:
            p = bin_path(name)
            entry = {"path": str(p), "sha256": sha256(p), "version": version_of(p)}
            repo = repo_of(p)
            if repo:
                entry["git"] = git_info(repo)
            info["inputs"][name] = entry
        except SystemExit:
            pass
    repo = Path(env("UNBIHEX_REPO", "")) if env("UNBIHEX_REPO") else None
    if repo and (repo / ".git").exists():
        info["git"] = {
            "commit": sh(f"git -C {repo} rev-parse HEAD").stdout.strip(),
            "branch": sh(f"git -C {repo} rev-parse --abbrev-ref HEAD").stdout.strip(),
            "dirty": sh(f"git -C {repo} status --porcelain").stdout.strip() != "",
        }
    return info


def bench(args, timeout=300):
    r = subprocess.run(f"{BENCH} {args}", shell=True, capture_output=True, text=True, timeout=timeout)
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows, r.stderr.strip()


def measure(scenario, rtt, quick, runs, label):
    port = socks_port(scenario)
    set_rtt(rtt)
    pids = []
    for role in ("server", "client"):
        f = pid_file(scenario, role)
        if f.exists():
            pids.append(int(f.read_text()))
    result = provenance(scenario, rtt)
    result["label"] = label
    result["metrics"] = {}

    def phase(name, args, cpu=False):
        before = (cpu_ticks(pids[:1]), cpu_ticks(pids[1:])) if cpu else None
        rows, err = bench(args)
        out = [r for r in rows if r.get("event") in ("summary", "conns")]
        summary = out[0] if out else {"error": err[:300]}
        if cpu:
            mib = sum(r.get("mib", 0) for r in rows if "run" in r)
            result["metrics"][name + "_cpu"] = {
                "server_ms": (cpu_ticks(pids[:1]) - before[0]) * 10,
                "client_ms": (cpu_ticks(pids[1:]) - before[1]) * 10,
                "mib": mib,
                "server_ms_per_mib": round((cpu_ticks(pids[:1]) - before[0]) * 10 / mib, 2) if mib else None,
                "client_ms_per_mib": round((cpu_ticks(pids[1:]) - before[1]) * 10 / mib, 2) if mib else None,
            }
        result["metrics"][name] = summary

    target = f"{VERSIONS['SINK_IP']}:{VERSIONS['SINK_PORT']}"
    # The first two connections through a fresh engine are not representative
    # (the stand sees a multi-second stall on the first one).
    for _ in range(2):
        bench(f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 32 --conns 1 --runs 1")

    phase("down1", f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 64 --conns 1 --runs {runs}", cpu=True)
    if not quick:
        phase("down8", f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 64 --conns 8 --runs {max(1, runs - 1)}", cpu=True)
        phase("up1", f"run --proxy 127.0.0.1:{port} --target {target} --mode up --mib 32 --conns 1 --runs {max(1, runs - 1)}", cpu=True)
    phase("ttfb", f"ttfb --proxy 127.0.0.1:{port} --target {target} --runs 30")
    if not quick:
        phase("conns", f"conns --proxy 127.0.0.1:{port} --target {target} --count 200 --par 4")

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{scenario}-rtt{rtt}-{label}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2))
    say(json.dumps({"scenario": scenario, "rtt": rtt, "label": label, **result["metrics"]}, indent=2))
    say(f"saved {path.relative_to(ROOT)}")
    return path


def check(scenario):
    """Negative controls: if the server is gone the measurement must fail."""
    port = socks_port(scenario)
    target = f"{VERSIONS['SINK_IP']}:{VERSIONS['SINK_PORT']}"
    ok = True
    srv = int(pid_file(scenario, "server").read_text())
    os.kill(srv, signal.SIGTERM)
    time.sleep(1.5)
    rows, err = bench(f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 1 --conns 1 --runs 1", timeout=60)
    failed = any("error" in r for r in rows) or not rows or "failed" in err
    say(("ok: " if failed else "FAIL: ") + "traffic fails when the server is stopped")
    ok &= failed
    up(scenario)
    rows, _ = bench(f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 8 --conns 1 --runs 1", timeout=60)
    works = any(r.get("mbps", 0) > 0 for r in rows)
    say(("ok: " if works else "FAIL: ") + "traffic works after restart")
    ok &= works
    sys.exit(0 if ok else 1)


# ------------------------------------------------------------------ DNS mode

DNSD = ROOT / "tools/dnsd.py"
DNS_ZONE = ROOT / "tools/dns-zone.json"
DNS_BASE_PORT = 15353
DNS_DEAD_PORT = 15354

# scenario -> (server template for DNS runs, config family)
DNS_SERVER_TMPL = {
    "xray_reality": ("xray-server-reality-dns.json.tmpl", "xray"),
    "xray_xhttp": ("xray-server-xhttp-reality-dns.json.tmpl", "xray"),
    "singbox_reality": ("singbox-server-reality-dns.json.tmpl", "singbox"),
    "unbihex_reality": ("unbihex-server-reality-dns.json.tmpl", "unbihex"),
    "xraycl_ubisrv": ("unbihex-server-reality-dns.json.tmpl", "unbihex"),
    "ubicl_xraysrv": ("xray-server-reality-dns.json.tmpl", "xray"),
}


def read_env(path):
    out = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out


def render(src, dst, vals):
    text = Path(src).read_text()

    def sub(m):
        if m.group(1) not in vals:
            sys.exit(f"template {src}: no value for @{m.group(1)}@")
        return vals[m.group(1)]

    Path(dst).write_text(re.sub(r"@([A-Z0-9_]+)@", sub, text))


def dns_config(scenario, upstreams):
    """Render the scenario's server config with dnsd as its only resolver."""
    tmpl, family = DNS_SERVER_TMPL[scenario]
    vals = dict(VERSIONS)
    vals.update(read_env(ROOT / ".state/keys.env"))
    if "PRIVATE_KEY" not in vals:
        sys.exit("missing .state/keys.env; run ./setup.sh first")
    vals["SINK_CIDR"] = VERSIONS["SINK_IP"] + "/24"
    vals["PORT"] = str(SCENARIOS[scenario]["ports"][0])
    hosts = [f"{host}:{port}" for host, port in upstreams]
    if family == "xray":
        # Xray 26 takes a host and a port separately; "host:port" is parsed as a URL.
        vals["DNS_SERVERS"] = ", ".join(
            '{"address":"%s","port":%s}' % (h.rsplit(":", 1)[0], h.rsplit(":", 1)[1])
            for h in hosts
        )
    elif family == "singbox":
        vals["DNS_SERVERS"] = ", ".join(
            f'{{"tag":"dnsd{i}","type":"udp","server":"{h.rsplit(":", 1)[0]}",'
            f'"server_port":{h.rsplit(":", 1)[1]}}}'
            for i, h in enumerate(hosts)
        )
        vals["DNS_FIRST"] = "dnsd0"
    else:
        vals["DNS_UPSTREAMS"] = ", ".join(f'"{h}"' for h in hosts)
    out = ROOT / "configs" / tmpl.replace(".tmpl", "")
    render(ROOT / "configs" / tmpl, out, vals)
    return out


def start_dnsd(tag, port, delay_ms=0, fail=False, refuse=False):
    stop_dnsd(tag)
    if sh(f"ss -lnt | grep -q ':{port} '").returncode == 0:
        sys.exit(f"dnsd ({tag}): port {port} is already in use by another process; "
                 f"stop it or pick another port")
    log = ROOT / "logs" / f"dnsd-{tag}.jsonl"
    stats = STATE / f"dnsd-{tag}.stats.json"
    for p in (log, stats):
        p.unlink(missing_ok=True)
    cmd = [sys.executable, str(DNSD), "--port", str(port), "--zone", str(DNS_ZONE),
           "--log", str(log), "--stats", str(stats)]
    if delay_ms:
        cmd += ["--delay-ms", str(delay_ms)]
    if fail:
        cmd.append("--fail")
    if refuse:
        cmd.append("--refuse")
    out = open(ROOT / "logs" / f"dnsd-{tag}.log", "w")
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    (PIDS / f"dnsd-{tag}.pid").write_text(str(p.pid))
    if not wait_port(port):
        sys.exit(f"dnsd ({tag}) did not listen on UDP/TCP {port}; "
                 f"see logs/dnsd-{tag}.log")
    return {"tag": tag, "port": port, "delay_ms": delay_ms,
            "mode": "fail" if fail else ("refuse" if refuse else "ok"),
            "log": log, "stats": stats}


def stop_dnsd(tag):
    f = PIDS / f"dnsd-{tag}.pid"
    if not f.exists():
        return None
    try:
        pid = int(f.read_text())
        os.kill(pid, signal.SIGTERM)
        end = time.time() + 3
        while time.time() < end and Path(f"/proc/{pid}").exists():
            time.sleep(0.02)
        if Path(f"/proc/{pid}").exists():
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    f.unlink(missing_ok=True)
    return STATE / f"dnsd-{tag}.stats.json"


def dnsd_stats(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {"total": 0, "by_qtype": {}, "names": []}


def log_offset(path):
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def log_queries(path, names=None, offset=0):
    """Count logged queries, optionally limited to names, skipping the first offset lines."""
    want = {n.lower().rstrip(".") for n in names} if names is not None else None
    counter = Counter()
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                if i < offset:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = row.get("name", "")
                if want is not None and name not in want:
                    continue
                counter[(name, row.get("qtype", "?"))] += 1
    except OSError:
        pass
    return counter


def summarize(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    p95 = vals[min(len(vals) - 1, max(0, math.ceil(0.95 * len(vals)) - 1))]
    return {
        "count": len(vals),
        "min": round(vals[0], 3),
        "median": round(vals[len(vals) // 2] if len(vals) % 2 else
                         (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2, 3),
        "p95": round(p95, 3),
        "max": round(vals[-1], 3),
    }


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def socks_probe(proxy_port, name, timeout=30.0, sink_port=None):
    """One SOCKS5 CONNECT to name:sink_port, then a ping byte through the tunnel.

    The ping matters: Unbihex answers a SOCKS CONNECT before it starts the
    upstream connection, so the reply alone would hide DNS and dial latency.
    """
    sink_port = int(sink_port or VERSIONS["SINK_PORT"])
    start = time.perf_counter()
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if recv_exact(s, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS method negotiation failed")
        raw = name.encode("idna")
        req = b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw + sink_port.to_bytes(2, "big")
        s.sendall(req)
        head = recv_exact(s, 4)
        if head[1] != 0:
            raise ConnectionError(f"SOCKS CONNECT refused (reply {head[1]})")
        atyp = head[3]
        if atyp == 0x01:
            recv_exact(s, 4)
        elif atyp == 0x04:
            recv_exact(s, 16)
        elif atyp == 0x03:
            recv_exact(s, recv_exact(s, 1)[0])
        recv_exact(s, 2)
        s.sendall(b"\x02")  # sink mode 2: ping
        if recv_exact(s, 1) != b"\x01":
            raise ConnectionError("sink did not answer the ping")
    return round((time.perf_counter() - start) * 1000, 3)


def dns_up(scenario, cfg):
    """Start the scenario with only its server config swapped for the DNS one."""
    clone = copy.deepcopy(SCENARIOS[scenario])
    for proc in clone["start"]:
        if proc["name"] == "server":
            args = list(proc["args"])
            args[args.index("-c") + 1] = str(cfg.relative_to(ROOT))
            proc["args"] = args
    SCENARIOS[scenario] = clone
    up(scenario)


def ensure_sink6():
    """The zone hands out ::1 for AAAA; the sink has to listen there too."""
    if sh("ss -lnt | grep -q '\\[::1\\]:19000'").returncode == 0:
        return
    log = open(ROOT / "logs/sink6.log", "w")
    p = subprocess.Popen([str(BENCH), "sink", "--listen", f"[::1]:{VERSIONS['SINK_PORT']}"],
                         cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    (PIDS / "sink6.pid").write_text(str(p.pid))
    time.sleep(0.3)


def save_dns_result(scenario, sub, n, label, rtt, dnsd_info, extra):
    result = provenance(scenario, rtt)
    result["kind"] = "dns"
    result["subcommand"] = sub
    result["n"] = n
    result["label"] = label
    result["engine"] = SCENARIOS[scenario]["start"][0]["bin"]
    result["dnsd"] = {
        "upstreams": [{"port": u["port"], "mode": u["mode"], "delay_ms": u["delay_ms"]}
                      for u in dnsd_info],
        "zone": str(DNS_ZONE.relative_to(ROOT)),
        "zone_names": len(json.loads(DNS_ZONE.read_text())["names"]),
    }
    result.update(extra)
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"dns-{scenario}-{sub}-{label}-{stamp}.json"
    path = RESULTS / name
    path.write_text(json.dumps(result, indent=2))
    say(json.dumps({"scenario": scenario, "sub": sub, "n": n, "label": label,
                    "latency_ms": result.get("latency_ms"),
                    "queries": result.get("queries"),
                    "checks": result.get("checks")}, indent=2))
    say(f"saved {path.relative_to(ROOT)}")
    return path


def dns_run(scenario, sub, n, delay_ms, label, rtt, timeout):
    if scenario not in SCENARIOS:
        sys.exit(f"unknown scenario {scenario}; known: {', '.join(SCENARIOS)}")
    if scenario not in DNS_SERVER_TMPL:
        sys.exit(f"{scenario}: no DNS server template; use one of "
                 f"{', '.join(DNS_SERVER_TMPL)}")
    if not (ROOT / ".state/keys.env").exists():
        say("no rendered configs yet; calling setup.sh")
        subprocess.run([str(ROOT / "setup.sh")], cwd=ROOT, check=True)
    if sub in ("cold", "warm") and n > 200:
        sys.exit(f"{sub}: the zone has 200 names; use N <= 200")
    RESULTS.mkdir(exist_ok=True)
    ensure_sink6()
    set_rtt(rtt)
    clean = []
    try:
        if sub == "dead":
            dnsd = [start_dnsd("dead", DNS_BASE_PORT, delay_ms, fail=True),
                    start_dnsd("live", DNS_DEAD_PORT)]
            upstreams = [("127.0.0.1", DNS_BASE_PORT), ("127.0.0.1", DNS_DEAD_PORT)]
        else:
            dnsd = [start_dnsd("live", DNS_BASE_PORT, delay_ms)]
            upstreams = [("127.0.0.1", DNS_BASE_PORT)]
        clean.extend(dnsd)
        cfg = dns_config(scenario, upstreams)
        dns_up(scenario, cfg)
        port = socks_port(scenario)
        log = dnsd[0]["log"]
        # Like measure(): the first connection(s) through a fresh engine include
        # startup noise. Warm up on a name the measurements do not count.
        if sub != "warm":
            for _ in range(2):
                try:
                    socks_probe(port, "n198.dns.test", timeout)
                except Exception:
                    pass

        if sub == "cold":
            names = [f"n{i % 200:03d}.dns.test" for i in range(max(1, n))]
            per = []
            for name in names:
                try:
                    per.append({"name": name, "ms": socks_probe(port, name, timeout)})
                except Exception as exc:
                    per.append({"name": name, "ms": None, "error": str(exc)[:200]})
            stop(scenario)
            stats = dnsd_stats(stop_dnsd("live"))
            counts = log_queries(log, names)
            extra = {
                "latency_ms": summarize([r["ms"] for r in per]),
                "per_name": per,
                "queries": {
                    "received": sum(counts.values()) > 0,
                    "total": sum(counts.values()),
                    "by_qtype": dict(Counter(q for _, q in counts.elements())),
                    "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                 for k, v in sorted(counts.items())],
                    "stats": stats,
                    "expected_per_name": 2,
                },
            }
            return save_dns_result(scenario, sub, len(names), label, rtt, dnsd, extra)

        if sub == "warm":
            names = [f"n{i % 200:03d}.dns.test" for i in range(max(1, n))]
            for name in names:  # prime the engine's cache
                socks_probe(port, name, timeout)
            offset = log_offset(log)
            per = []
            for name in names:
                try:
                    per.append({"name": name, "ms": socks_probe(port, name, timeout)})
                except Exception as exc:
                    per.append({"name": name, "ms": None, "error": str(exc)[:200]})
            stop(scenario)
            stats = dnsd_stats(stop_dnsd("live"))
            prime = log_queries(log, names)
            measured = log_queries(log, names, offset)
            extra = {
                "latency_ms": summarize([r["ms"] for r in per]),
                "per_name": per,
                "queries": {
                    "received": sum(prime.values()) > 0,
                    "total": sum(prime.values()),
                    "measured_total": sum(measured.values()),
                    "by_qtype": dict(Counter(q for _, q in prime.elements())),
                    "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                 for k, v in sorted(prime.items())],
                    "stats": stats,
                    "expected_measured": 0,
                },
                "checks": {"warm_has_no_upstream_queries":
                           sum(measured.values()) == 0},
                "note": ("measured_total > 0 means the engine re-queried: with this "
                         "zone the odd names have no AAAA, and NODATA answers are "
                         "not always negatively cached"),
            }
            return save_dns_result(scenario, sub, len(names), label, rtt, dnsd, extra)

        if sub == "stampede":
            name = "n150.dns.test"
            per = [None] * max(1, n)
            errors = [None] * max(1, n)
            barrier = threading.Barrier(max(1, n))

            def one(i):
                try:
                    barrier.wait(timeout=10)
                    per[i] = socks_probe(port, name, timeout)
                except Exception as exc:
                    errors[i] = str(exc)[:200]

            threads = [threading.Thread(target=one, args=(i,)) for i in range(max(1, n))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop(scenario)
            stats = dnsd_stats(stop_dnsd("live"))
            counts = log_queries(log, [name])
            total = sum(counts.values())
            extra = {
                "latency_ms": summarize(per),
                "per_name": [{"name": name, "ms": per[i], "error": errors[i]}
                             for i in range(len(per))],
                "queries": {
                    "received": total > 0,
                    "total": total,
                    "by_qtype": dict(Counter(q for _, q in counts.elements())),
                    "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                 for k, v in sorted(counts.items())],
                    "stats": stats,
                    "expected": "1-2 (not one per connection)",
                },
                "checks": {"single_flight": total <= 2,
                           "all_connected": all(x is not None for x in per)},
            }
            return save_dns_result(scenario, sub, len(per), label, rtt, dnsd, extra)

        if sub == "dead":
            names = [f"n{160 + i}.dns.test" for i in range(3)]
            per = []
            for name in names:
                try:
                    per.append({"name": name, "ms": socks_probe(port, name, timeout)})
                except Exception as exc:
                    per.append({"name": name, "ms": None, "error": str(exc)[:200]})
            stop(scenario)
            dead_stats = dnsd_stats(stop_dnsd("dead"))
            live_stats = dnsd_stats(stop_dnsd("live"))
            dead_counts = log_queries(dnsd[0]["log"], names)
            live_counts = log_queries(dnsd[1]["log"], names)
            successful = [r["ms"] for r in per if r["ms"] is not None]
            extra = {
                "latency_ms": summarize(successful),
                "per_name": per,
                "queries": {
                    "received": bool(dead_counts) or bool(live_counts),
                    "dead": {"total": sum(dead_counts.values()),
                             "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                          for k, v in sorted(dead_counts.items())],
                             "stats": dead_stats},
                    "live": {"total": sum(live_counts.values()),
                             "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                          for k, v in sorted(live_counts.items())],
                             "stats": live_stats},
                },
                "checks": {"survives_one_dead_resolver": len(successful) == len(names)},
            }
            return save_dns_result(scenario, sub, len(names), label, rtt, dnsd, extra)

        if sub == "leak":
            name1 = "n190.dns.test"
            name2 = "n191.dns.test"
            first = None
            try:
                first = socks_probe(port, name1, timeout)
            except Exception as exc:
                first = f"error: {exc}"[:200]
            counts = log_queries(log, [name1])
            received = sum(counts.values()) > 0
            stop_dnsd("live")
            time.sleep(0.3)
            second = None
            try:
                second = socks_probe(port, name2, min(timeout, 12))
            except Exception as exc:
                second = f"error: {str(exc)[:150]}"
            stop(scenario)
            leaked = not isinstance(second, str)
            ok = received and not leaked
            extra = {
                "latency_ms": summarize([first] if isinstance(first, float) else []),
                "per_name": [{"name": name1, "ms": first},
                             {"name": name2, "ms": second}],
                "queries": {
                    "received": received,
                    "total": sum(counts.values()),
                    "per_name": [{"name": k[0], "qtype": k[1], "count": v}
                                 for k, v in sorted(counts.items())],
                },
                "checks": {
                    "dnsd_received_queries": received,
                    "fails_without_dnsd": not leaked,
                    "os_resolver_leak": leaked,
                },
            }
            path = save_dns_result(scenario, sub, 2, label, rtt, dnsd, extra)
            say(("ok: " if received else "FAIL: ") +
                "the engine's own resolver queried dnsd")
            say(("ok: " if not leaked else "FAIL: ") +
                "a fresh name fails when dnsd is stopped (no OS resolver fallback)")
            if not ok:
                sys.exit(1)
            return path

        sys.exit(f"unknown DNS subcommand {sub}")
    finally:
        stop(scenario)
        for info in clean:
            stop_dnsd(info["tag"])
        set_rtt(0)


def stop_dnsd_all():
    for f in list(PIDS.glob("dnsd-*.pid")) + [PIDS / "sink6.pid"]:
        if not f.exists():
            continue
        try:
            os.kill(int(f.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        f.unlink(missing_ok=True)


def report(pattern="*.json", markdown=False, files=None):
    rows = []
    dns_rows = []
    for f in (files if files is not None else sorted(RESULTS.glob(pattern))):
        d = json.loads(f.read_text())
        commit = (d.get("git") or {}).get("commit", "")[:8]
        if d.get("kind") == "dns":
            q = d.get("queries") or {}
            lat = d.get("latency_ms") or {}
            checks = d.get("checks") or {}
            n = d.get("n") or 0
            total = q.get("total")
            if total is None:
                total = sum(v.get("total", 0) for v in q.values()
                            if isinstance(v, dict))
            checks_ok = all(checks.values()) if checks else None
            if "os_resolver_leak" in checks:
                checks_ok = bool(checks.get("dnsd_received_queries") and
                                 checks.get("fails_without_dnsd"))
            dns_rows.append({
                "scenario": d.get("scenario", "?"),
                "sub": d.get("subcommand", "?"),
                "n": n,
                "p50": lat.get("median"),
                "p95": lat.get("p95"),
                "queries": total,
                "per_name": round(total / n, 2) if n else None,
                "received": "yes" if q.get("received") else "no",
                "checks": (("ok" if checks_ok else "FAIL")
                           if checks_ok is not None else "—"),
                "label": d.get("label", ""),
                "commit": commit,
                "file": f.name,
            })
            continue
        m = d.get("metrics", {})
        rows.append({
            "scenario": d["scenario"],
            "rtt": d["rtt_ms"],
            "label": d.get("label", ""),
            "down1": (m.get("down1") or {}).get("median_mbps"),
            "down8": (m.get("down8") or {}).get("median_mbps"),
            "up1": (m.get("up1") or {}).get("median_mbps"),
            "ttfb": (m.get("ttfb") or {}).get("median_ms"),
            "conns": (m.get("conns") or {}).get("conns_per_sec"),
            "srv_cpu": (m.get("down1_cpu") or {}).get("server_ms_per_mib"),
            "commit": commit,
            "file": f.name,
        })
    if not rows and not dns_rows:
        sys.exit("no results yet")
    fmt = lambda v: "—" if v is None else f"{v:.1f}" if isinstance(v, float) else str(v)
    blocks = []
    if rows:
        lines = []
        lines.append("| scenario | rtt | label | ↓1 MB/s | ↓8 MB/s | ↑1 MB/s | TTFB ms | conn/s | srv ms/MiB | commit |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            lines.append(f"| {r['scenario']} | {r['rtt']} | {r['label']} | {fmt(r['down1'])} | {fmt(r['down8'])} | "
                         f"{fmt(r['up1'])} | {fmt(r['ttfb'])} | {fmt(r['conns'])} | {fmt(r['srv_cpu'])} | {r['commit']} |")
        blocks.append("\n".join(lines))
    if dns_rows:
        lines = []
        lines.append("| dns scenario | sub | n | p50 ms | p95 ms | upstream queries | q/op | dnsd hit | checks | label | commit |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in dns_rows:
            lines.append(f"| {r['scenario']} | {r['sub']} | {r['n']} | {fmt(r['p50'])} | {fmt(r['p95'])} | "
                         f"{r['queries']} | {fmt(r['per_name'])} | {r['received']} | {r['checks']} | "
                         f"{r['label']} | {r['commit']} |")
        blocks.append("\n".join(lines))
    table = "\n\n".join(blocks)
    say(table)
    if markdown:
        out = ROOT / "report.md"
        out.write_text("# Stand results\n\n" + table +
                       "\n\nMeasured on loopback: see README for the caveats.\n")
        say(f"\nwritten {out.relative_to(ROOT)}")



# ------------------------------------------------------------------ versions

GH_LATEST = {
    "xray": ("XTLS/Xray-core", "XRAY_VERSION"),
    "singbox": ("SagerNet/sing-box", "SINGBOX_VERSION"),
}


def latest_github(repo):
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["tag_name"].lstrip("v")
    except Exception as e:  # offline, rate limit, no releases
        return f"unavailable ({type(e).__name__})"


def versions(update):
    """Compare the pinned stand inputs with the latest upstream releases."""
    rows = []
    for name, (repo, key) in GH_LATEST.items():
        pinned = VERSIONS[key].lstrip("v")
        latest = latest_github(repo)
        state = "up to date" if latest == pinned else "NEW"
        rows.append((name, pinned, latest, state, repo, key))
    # Unbihex: the built binary versus the newest tag and origin/main.
    repo_dir = env("UNBIHEX_REPO", "")
    if repo_dir and Path(repo_dir, ".git").exists():
        sh(f"git -C {repo_dir} fetch --tags --quiet origin")
        tags = sh(f"git -C {repo_dir} tag --list 'v*' --sort=-v:refname").stdout.split()
        head = sh(f"git -C {repo_dir} rev-parse --short HEAD").stdout.strip()
        behind = sh(f"git -C {repo_dir} rev-list --count HEAD..origin/main").stdout.strip()
        ahead = sh(f"git -C {repo_dir} rev-list --count origin/main..HEAD").stdout.strip()
        latest = tags[0].lstrip("v") if tags else "?"
        if behind == "0" and ahead == "0":
            state = "origin/main"
        elif behind == "0":
            state = f"{ahead} commits ahead of origin/main"
        else:
            state = f"{behind} commits behind origin/main"
        rows.append(("unbihex", head, f"{latest} (tag)", state, repo_dir, ""))

    say("| engine | pinned / built | latest | state |")
    say("|---|---|---|---|")
    for name, pinned, latest, state, _, _ in rows:
        say(f"| {name} | {pinned} | {latest} | {state} |")

    if update:
        changed = False
        for name, pinned, latest, state, repo, key in rows:
            if state != "NEW":
                continue
            say(f"updating {name}: {pinned} -> {latest}")
            # Download, hash, rewrite the pin; setup.sh re-fetches on the next run.
            if name == "xray":
                url = f"https://github.com/{repo}/releases/download/v{latest}/Xray-linux-64.zip"
                path = ROOT / "cache/xray-new.zip"
            else:
                url = (f"https://github.com/{repo}/releases/download/v{latest}/"
                       f"sing-box-{latest}-linux-amd64.tar.gz")
                path = ROOT / "cache/singbox-new.tar.gz"
            sh(f"curl -fsSL -o {path} {url}", check=True)
            digest = sha256(path)
            text = (ROOT / "versions.env").read_text()
            text = text.replace(f"{key}=v{pinned}", f"{key}=v{latest}")
            sha_key = "XRAY_SHA256" if name == "xray" else "SINGBOX_SHA256"
            text = re.sub(rf"^{sha_key}=.*$", f"{sha_key}={digest}", text, flags=re.M)
            (ROOT / "versions.env").write_text(text)
            path.unlink()
            changed = True
        if changed:
            say("pins updated; cache/ was kept — delete the old archive or run ./setup.sh "
                "after removing cache/ to re-download")
        else:
            say("nothing to update")
    return 0


# ------------------------------------------------------------------ matrices

DEFAULT_SCENARIOS = ["xray_reality", "xray_xhttp", "singbox_reality", "unbihex_reality",
                     "xraycl_ubisrv", "ubicl_xraysrv"]


def bench_matrix(scenarios, rtts, quick, runs, control):
    produced = []
    if control:
        say("== negative control ==")
        up(scenarios[0])
        check_inline(scenarios[0])
    for rtt in rtts:
        for scenario in scenarios:
            say(f"== {scenario}, RTT {rtt} ms ==")
            up(scenario)
            produced.append(measure(scenario, rtt, quick, runs, label=""))
            set_rtt(0)
    return produced


def check_inline(scenario):
    """Traffic must stop when the server stops: catches a direct leak."""
    port = socks_port(scenario)
    target = f"{VERSIONS['SINK_IP']}:{VERSIONS['SINK_PORT']}"
    srv = int(pid_file(scenario, "server").read_text())
    os.kill(srv, signal.SIGTERM)
    time.sleep(1.5)
    rows, err = bench(f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 1 --conns 1 --runs 1", timeout=60)
    ok = (not rows) or any("error" in r for r in rows) or "failed" in err
    say(("ok: " if ok else "FAIL: ") + "no traffic with the server stopped")
    up(scenario)
    if not ok:
        sys.exit(1)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("up"); p.add_argument("scenario")
    p = sub.add_parser("down"); p.add_argument("scenario", nargs="?")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("scenario"); p.add_argument("rtt", type=int, nargs="?", default=0)
    p.add_argument("--quick", action="store_true"); p.add_argument("--runs", type=int, default=3)
    p.add_argument("--label", default="")
    p = sub.add_parser("ab")
    p.add_argument("a"); p.add_argument("b"); p.add_argument("rtt", type=int, nargs="?", default=0)
    p.add_argument("--rounds", type=int, default=3); p.add_argument("--quick", action="store_true")
    p = sub.add_parser("report"); p.add_argument("pattern", nargs="?", default="*.json")
    p = sub.add_parser("check"); p.add_argument("scenario")
    p = sub.add_parser("dns")
    p.add_argument("scenario")
    p.add_argument("subcommand", choices=["cold", "warm", "stampede", "dead", "leak"])
    p.add_argument("n", nargs="?", type=int, default=20)
    p.add_argument("--delay-ms", type=int, default=0)
    p.add_argument("--rtt", type=int, default=0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--label", default="")
    p = sub.add_parser("versions"); p.add_argument("--update", action="store_true")
    p = sub.add_parser("table"); p.add_argument("pattern", nargs="?", default="*.json")
    p = sub.add_parser("bench")
    p.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    p.add_argument("--rtt", default="0,50", help="comma separated, e.g. 0,50")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--control", action="store_true", default=True)
    p.add_argument("--no-control", dest="control", action="store_false")
    args = ap.parse_args()

    if args.cmd == "up":
        up(args.scenario)
    elif args.cmd == "down":
        targets = list(SCENARIOS) if args.all or not args.scenario else [args.scenario]
        for s in targets:
            stop(s)
        if args.all or not args.scenario:
            stop_dnsd_all()
        set_rtt(0)
    elif args.cmd == "run":
        measure(args.scenario, args.rtt, args.quick, args.runs, args.label)
    elif args.cmd == "ab":
        for i in range(args.rounds):
            for s in (args.a, args.b):
                stop(s); up(s)
                measure(s, args.rtt, args.quick, 1, f"{s}-r{i + 1}")
    elif args.cmd == "report":
        report(args.pattern)
    elif args.cmd == "check":
        check(args.scenario)
    elif args.cmd == "dns":
        dns_run(args.scenario, args.subcommand, args.n, args.delay_ms,
                args.label, args.rtt, args.timeout)
    elif args.cmd == "versions":
        sys.exit(versions(args.update))
    elif args.cmd == "table":
        report(args.pattern, markdown=True)
    elif args.cmd == "bench":
        scenarios = [s for s in args.scenarios.split(",") if s]
        for s in scenarios:
            if s not in SCENARIOS:
                sys.exit(f"unknown scenario {s}")
        rtts = [int(x) for x in args.rtt.split(",") if x.strip()]
        versions(False)
        produced = bench_matrix(scenarios, rtts, args.quick, args.runs, args.control)
        report(markdown=True, files=produced)


if __name__ == "__main__":
    main()
