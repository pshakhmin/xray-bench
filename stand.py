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
                                stampede, dead, leak, soak (see tools/dnsd.py)
                                soak = N names, two passes, cache/RSS knobs

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

sys.path.insert(0, str(ROOT / "tools"))
import rss as rssmem  # noqa: E402  (VmRSS sampling + a big zone)

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
    "unbihex-server-release": lambda: env("UNBIHEX_SERVER_RELEASE", ""),
    "unbihex-cli-release": lambda: env("UNBIHEX_CLI_RELEASE", ""),
}


def bin_path(name):
    b = BINS[name]
    raw = b() if callable(b) else b
    if not str(raw).strip():
        sys.exit(f"missing binary {name}: not configured (run setup.sh, or set UNBIHEX_REPO)")
    p = Path(raw)
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


LINK_STATE = STATE / "link.json"


def _lo_mtu():
    r = sh("ip -o link show dev lo")
    m = re.search(r"\bmtu (\d+)", r.stdout)
    return int(m.group(1)) if m else None


def _lo_offloads():
    """Current gso/tso/gro state, or 'unavailable' when ethtool cannot tell."""
    r = sh("ethtool -k lo")
    if r.returncode != 0 or not r.stdout.strip():
        return {k: "unavailable" for k in ("gso", "tso", "gro")}
    table = {"gso": "generic-segmentation-offload",
             "tso": "tcp-segmentation-offload",
             "gro": "generic-receive-offload"}
    out = {}
    for key, name in table.items():
        m = re.search(rf"^{name}:\s+(on|off)", r.stdout, re.M)
        out[key] = m.group(1) if m else "unavailable"
    return out


def _load_original_link():
    """Capture lo's real MTU/offloads once, before the first profile is applied."""
    try:
        d = json.loads(LINK_STATE.read_text())
        if isinstance(d, dict) and "mtu" in d:
            return d
    except (OSError, json.JSONDecodeError):
        pass
    d = {"mtu": _lo_mtu(), **_lo_offloads()}
    try:
        STATE.mkdir(exist_ok=True)
        LINK_STATE.write_text(json.dumps(d, indent=2))
    except OSError:
        pass
    return d


def restore_link():
    """Undo the qdisc, MTU and offload changes; safe when nothing was applied."""
    sh("tc qdisc del dev lo root 2>/dev/null")
    try:
        d = json.loads(LINK_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    mtu = d.get("mtu")
    if isinstance(mtu, int):
        sh(f"ip link set dev lo mtu {mtu}")
    if shutil.which("ethtool"):
        args = [f"{k} {d[k]}" for k in ("gso", "tso", "gro")
                if d.get(k) in ("on", "off")]
        if args:
            sh("ethtool -K lo " + " ".join(args))


def apply_link(profile, rtt, loss=None, rate=None):
    """Shape lo like a real link and return the applied settings for the result.

    loopback: MTU 65536, offloads on, delay only — today's behaviour.
    wan:      MTU 1500, offloads off, delay=rtt/2 per direction, 2 ms jitter,
              0.1% loss, no rate cap unless --rate is given. A rate cap is
              implemented through netem and flattens throughput differences
              between engines by design.
    """
    sh("tc qdisc del dev lo root 2>/dev/null")
    profile = profile or "loopback"
    _load_original_link()
    if profile == "wan":
        mtu, want_offload = 1500, False
        # Jitter without delay is pure reordering: on a 0 ms link it pins cwnd at
        # the initial window (measured: 2.8 MB/s on a 4-vCPU runner instead of
        # >100). Apply it only when there is a delay for it to perturb.
        jitter_ms = min(2, max(0, rtt // 2 // 5)) if rtt > 0 else 0
        loss_pct = 0.1 if loss is None else float(loss)
    else:
        mtu, want_offload = 65536, True
        jitter_ms = 0
        loss_pct = 0.0 if loss is None else float(loss)
    rate_mbit = 0.0 if rate is None else float(rate)
    sh(f"ip link set dev lo mtu {mtu}")
    gso = "unavailable"
    if shutil.which("ethtool"):
        want = "on" if want_offload else "off"
        if sh(f"ethtool -K lo gso {want} tso {want} gro {want}").returncode == 0:
            gso = want
    half = rtt // 2 if rtt > 0 else 0
    # A wan profile keeps its jitter/loss even at RTT 0; loopback at RTT 0 stays
    # a plain noqueue lo unless the caller asked for loss/rate explicitly.
    if rtt > 0 or jitter_ms or loss_pct or rate_mbit:
        opts = f"delay {half}ms"
        if jitter_ms:
            opts += f" {jitter_ms}ms"
        if loss_pct:
            opts += f" loss {loss_pct:g}%"
        if rate_mbit:
            opts += f" rate {rate_mbit:g}mbit"
        opts += " limit 20000"
        sh("tc qdisc add dev lo root handle 1: prio bands 4 priomap " + " ".join(["0"] * 16))
        sh(f"tc qdisc add dev lo parent 1:2 handle 20: netem {opts}")
        sh(f"tc qdisc add dev lo parent 1:3 handle 30: netem {opts}")
        for p in tunnel_ports():
            sh(f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport {p} 0xffff flowid 1:2")
            sh(f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip sport {p} 0xffff flowid 1:3")
    return {
        "profile": profile,
        "one_way_ms": half,
        "jitter_ms": jitter_ms,
        "loss_pct": loss_pct,
        "rate_mbit": rate_mbit,
        "mtu": mtu,
        "gso": gso,
    }


def set_rtt(ms):
    """Backwards-compatible shim for callers that only know about RTT.

    ms == 0 fully restores lo (qdisc, MTU, offloads); ms > 0 applies the
    delay-only loopback profile.
    """
    if ms <= 0:
        restore_link()
        return
    apply_link("loopback", ms)


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


def read_cpu_stat():
    """Aggregate CPU counters in clock ticks; /proc/stat's cpu line sums all CPUs."""
    try:
        with open("/proc/stat") as f:
            line = f.readline()
    except OSError:
        return None
    if not line.startswith("cpu "):
        return None
    try:
        vals = [int(x) for x in line.split()[1:9]]
    except ValueError:
        return None
    while len(vals) < 8:
        vals.append(0)
    user, nice, system, idle, iowait, irq, softirq, steal = vals[:8]
    return {"user": user, "nice": nice, "system": system, "idle": idle,
            "iowait": iowait, "irq": irq, "softirq": softirq, "steal": steal,
            "total": user + nice + system + idle + iowait + irq + softirq + steal}


def read_meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    try:
                        out[key] = int(rest.split()[0])
                    except (ValueError, IndexError):
                        pass
    except OSError:
        pass
    return out


def proc_rss(pid):
    vals = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    try:
                        vals["rss_kb"] = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("VmHWM:"):
                    try:
                        vals["hwm_kb"] = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
    except OSError:
        return None
    return vals or None


def sys_snapshot(cpu0, mem0, role_pids, sample=None):
    """System CPU/RAM used during a phase plus the engines' resident memory."""
    cpu1 = read_cpu_stat()
    mem1 = read_meminfo()
    out = {}
    if cpu0 and cpu1:
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            hz = 100
        to_ms = lambda v: int(round(v * 1000.0 / hz))
        d = {k: cpu1.get(k, 0) - cpu0.get(k, 0) for k in cpu0}
        out["cpu"] = {
            "busy_ms": to_ms(d["total"] - d["idle"] - d["iowait"]),
            "user_ms": to_ms(d["user"] + d["nice"]),
            "system_ms": to_ms(d["system"]),
            "softirq_ms": to_ms(d["softirq"]),
            "irq_ms": to_ms(d["irq"]),
            "nproc": os.cpu_count(),
        }
    ram = {
        "mem_total_kb": mem1.get("MemTotal", mem0.get("MemTotal")),
        "mem_available_before_kb": mem0.get("MemAvailable"),
        "mem_available_after_kb": mem1.get("MemAvailable"),
    }
    if isinstance(ram["mem_total_kb"], int) and isinstance(ram["mem_available_after_kb"], int):
        ram["used_kb"] = ram["mem_total_kb"] - ram["mem_available_after_kb"]
    if sample is not None and sample.available_min_kb is not None:
        ram["available_min_kb"] = sample.available_min_kb
    out["ram"] = ram
    rss = {}
    for role, ps in role_pids.items():
        for pid in ps:
            v = proc_rss(pid)
            if v:
                rss[role] = v
                break
    if sample is not None:
        for role, peak in sample.rss_max.items():
            rss.setdefault(role, {})["rss_max_kb"] = peak
    if rss:
        out["engine_rss_kb"] = rss
    return out


def qdisc_evidence():
    """One tc call per phase: queue counters and a short raw dump."""
    r = sh("tc -s qdisc show dev lo")
    text = r.stdout.strip()
    ev = {"dropped": 0, "overlimits": 0, "requeues": 0, "packets": 0,
          "bytes": 0, "backlog": 0}
    for m in re.finditer(r"Sent (\d+) bytes (\d+) pkt "
                         r"\(dropped (\d+), overlimits (\d+) requeues (\d+)\)", text):
        ev["bytes"] += int(m.group(1))
        ev["packets"] += int(m.group(2))
        ev["dropped"] += int(m.group(3))
        ev["overlimits"] += int(m.group(4))
        ev["requeues"] += int(m.group(5))
    for m in re.finditer(r"backlog (\d+)b", text):
        ev["backlog"] = max(ev["backlog"], int(m.group(1)))
    ev["qdisc_raw"] = "\n".join(text.splitlines()[:20])
    return ev


def _drop_rate(dropped, packets):
    return round(dropped / packets * 100, 2) if packets else 0.0


def qdisc_delta(before, after):
    """Per-phase delta of the cumulative qdisc counters, plus the raw totals."""
    ev = {k: max(0, after.get(k, 0) - before.get(k, 0))
          for k in ("dropped", "overlimits", "requeues", "packets", "bytes")}
    ev["drop_rate_pct"] = _drop_rate(ev["dropped"], ev["packets"])
    ev["backlog_max"] = max(before.get("backlog", 0), after.get("backlog", 0))
    total = {k: after.get(k) for k in
             ("dropped", "overlimits", "requeues", "packets", "bytes", "backlog")}
    total["drop_rate_pct"] = _drop_rate(total["dropped"], total["packets"])
    ev["qdisc_total"] = total
    ev["qdisc_raw"] = after.get("qdisc_raw", "")
    return ev


def _parse_sock(lines, ports):
    """Parse one `ss -tin` dump: matching ESTAB blocks, retrans, rtt and cwnd.

    Both tunnel endpoints are local on this stand, so one connection shows up as
    two ESTAB rows (client side and server side). Count unique port pairs, not
    rows; retrans/rtt/cwnd/raw still come from every matching row.
    """
    keep = []
    sockets = 0
    seen = set()
    retrans = 0
    rtt = None
    cwnd = None

    def port_of(addr):
        try:
            return int(addr.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            return None

    i = 0
    while i < len(lines):
        parts = lines[i].split()
        lp = port_of(parts[3]) if len(parts) > 4 else None
        pp = port_of(parts[4]) if len(parts) > 4 else None
        if parts and parts[0] == "ESTAB" and (lp in ports or pp in ports):
            key = frozenset((lp, pp))
            if key not in seen:
                seen.add(key)
                sockets += 1
            block = [lines[i]]
            j = i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")):
                block.append(lines[j])
                j += 1
            blob = "\n".join(block)
            for m in re.finditer(r"retrans:(\d+)/", blob):
                retrans += int(m.group(1))
            m = re.search(r"\brtt:([\d.]+)/", blob)
            if m:
                try:
                    rtt = float(m.group(1))
                except ValueError:
                    pass
            m = re.search(r"\bcwnd:(\d+)", blob)
            if m:
                cwnd = int(m.group(1))
            if len(keep) < 40:
                keep.extend(block[:40 - len(keep)])
            i = j
            continue
        i += 1
    return {"sockets": sockets, "retrans": retrans, "rtt_ms": rtt, "cwnd": cwnd,
            "sock_raw": "\n".join(keep[:40])}


def sock_evidence():
    """Post-phase fallback when the sampler never saw a socket."""
    s = _parse_sock(sh("ss -tin").stdout.splitlines(), set(tunnel_ports()))
    return {"sockets": s["sockets"], "retrans": s["retrans"], "sock_raw": s["sock_raw"]}


class PhaseSampler(threading.Thread):
    """Poll sockets, meminfo and engine RSS while the measured phase runs.

    The transfer lasts hundreds of milliseconds, so sockets are already gone by
    the time bench() returns; polling at ~4 Hz keeps the evidence in-phase.
    """

    def __init__(self, role_pids, interval=0.25):
        super().__init__(daemon=True)
        self.role_pids = role_pids
        self.interval = interval
        self._stop_event = threading.Event()
        self.ports = set(tunnel_ports())
        self.sockets_max = 0
        self.retrans_total = 0
        self.rtt_ms_last = None
        self.cwnd_last = None
        self.sock_raw = ""
        self.available_min_kb = None
        self.rss_max = {}

    def sample(self):
        s = _parse_sock(sh("ss -tin").stdout.splitlines(), self.ports)
        self.sockets_max = max(self.sockets_max, s["sockets"])
        self.retrans_total = max(self.retrans_total, s["retrans"])
        if s["rtt_ms"] is not None:
            self.rtt_ms_last = s["rtt_ms"]
        if s["cwnd"] is not None:
            self.cwnd_last = s["cwnd"]
        if s["sock_raw"]:
            self.sock_raw = s["sock_raw"]
        avail = read_meminfo().get("MemAvailable")
        if isinstance(avail, int):
            self.available_min_kb = (avail if self.available_min_kb is None
                                     else min(self.available_min_kb, avail))
        for role, ps in self.role_pids.items():
            for pid in ps:
                v = proc_rss(pid)
                if v and "rss_kb" in v:
                    self.rss_max[role] = max(self.rss_max.get(role, 0), v["rss_kb"])
                    break

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.sample()
            except Exception:
                pass
            self._stop_event.wait(self.interval)
        try:
            self.sample()  # final sample, taken just before the thread is joined
        except Exception:
            pass

    def stop(self):
        self._stop_event.set()
        self.join(timeout=max(2.0, self.interval * 3))

    def evidence(self):
        if self.sockets_max or self.sock_raw:
            return {"sockets": self.sockets_max,
                    "retrans": self.retrans_total,
                    "rtt_ms_last": self.rtt_ms_last,
                    "cwnd_last": self.cwnd_last,
                    "sock_raw": self.sock_raw}
        return sock_evidence()


def evaluate_validity(metrics):
    """A measurement is only comparable if traffic actually flowed cleanly."""
    reasons = []
    if not isinstance(metrics, dict):
        return False, ["no metrics"]
    for name, m in metrics.items():
        if name.endswith("_cpu"):
            continue
        if isinstance(m, dict) and m.get("error"):
            reasons.append(f"{name}: error")
    ttfb = metrics.get("ttfb") or {}
    if isinstance(ttfb, dict):
        try:
            if int(ttfb.get("errors") or 0) > 0:
                reasons.append(f"ttfb.errors={ttfb.get('errors')}")
        except (TypeError, ValueError):
            pass
    conns = metrics.get("conns") or {}
    if isinstance(conns, dict) and conns.get("count") is not None:
        try:
            if int(conns.get("success", 0)) < int(conns["count"]):
                reasons.append(f"conns.success={conns.get('success')} < count={conns['count']}")
        except (TypeError, ValueError):
            pass
    down1 = metrics.get("down1") or {}
    med = down1.get("median_mbps") if isinstance(down1, dict) else None
    if med is None:
        reasons.append("down1.median_mbps missing")
    else:
        try:
            med = float(med)
            if med < 2:
                reasons.append(f"down1.median_mbps={med} < 2")
            mn, mx = down1.get("min_mbps"), down1.get("max_mbps")
            if mn is not None and mx is not None and med > 0:
                spread = (float(mx) - float(mn)) / med
                if spread > 0.6:
                    reasons.append(f"down1 spread {spread:.2f} > 0.6")
        except (TypeError, ValueError):
            pass
    return (not reasons), reasons


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
    for name, key in (("xray", "xray"), ("singbox", "singbox"),
                      ("unbihex-server", "unbihex-server"), ("unbihex-cli", "unbihex-cli"),
                      # release installs expose the same engines under separate keys;
                      # record them under the canonical name so provenance is uniform
                      ("unbihex-server-release", "unbihex-server"),
                      ("unbihex-cli-release", "unbihex-cli")):
        try:
            p = bin_path(name)
            entry = {"path": str(p), "sha256": sha256(p), "version": version_of(p)}
            repo = repo_of(p)
            if repo:
                entry["git"] = git_info(repo)
            info["inputs"][key] = entry
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


def measure(scenario, rtt, quick, runs, label, profile="wan", loss=None, rate=None):
    port = socks_port(scenario)
    link = apply_link(profile, rtt, loss, rate)
    pids = []
    for role in ("server", "client"):
        f = pid_file(scenario, role)
        if f.exists():
            pids.append(int(f.read_text()))
    role_pids = {"server": pids[:1], "client": pids[1:]}
    result = provenance(scenario, rtt)
    result["label"] = label
    result["link"] = link
    result["metrics"] = {}
    mem_start = read_meminfo()
    result["machine"] = {
        "cpu_model": result.get("cpu"),
        "nproc": os.cpu_count(),
        "mem_total_kb": mem_start.get("MemTotal"),
        "mem_available_kb_at_start": mem_start.get("MemAvailable"),
    }

    def phase(name, args, cpu=False, runs_value=None):
        before = (cpu_ticks(pids[:1]), cpu_ticks(pids[1:])) if cpu else None
        cpu0 = read_cpu_stat()
        mem0 = read_meminfo()
        q_before = qdisc_evidence()
        sampler = PhaseSampler(role_pids)
        sampler.start()
        try:
            rows, err = bench(args)
        finally:
            sampler.stop()
        q_after = qdisc_evidence()
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
        if runs_value is not None:
            summary["runs"] = runs_value
        summary["sys"] = sys_snapshot(cpu0, mem0, role_pids, sampler)
        summary["evidence"] = {"qdisc": qdisc_delta(q_before, q_after),
                               "sock": sampler.evidence()}
        result["metrics"][name] = summary

    target = f"{VERSIONS['SINK_IP']}:{VERSIONS['SINK_PORT']}"
    # The first two connections through a fresh engine are not representative
    # (the stand sees a multi-second stall on the first one).
    for _ in range(2):
        bench(f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 32 --conns 1 --runs 1")

    phase("down1", f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 64 --conns 1 --runs {runs}", cpu=True, runs_value=runs)
    if not quick:
        phase("down8", f"run --proxy 127.0.0.1:{port} --target {target} --mode down --mib 64 --conns 8 --runs {runs}", cpu=True, runs_value=runs)
        phase("up1", f"run --proxy 127.0.0.1:{port} --target {target} --mode up --mib 32 --conns 1 --runs {runs}", cpu=True, runs_value=runs)
    phase("ttfb", f"ttfb --proxy 127.0.0.1:{port} --target {target} --runs 30")
    if not quick:
        phase("conns", f"conns --proxy 127.0.0.1:{port} --target {target} --count 200 --par 4")

    result["valid"], result["invalid_reasons"] = evaluate_validity(result["metrics"])

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
    "unbihex_release": ("unbihex-testserver-reality-dns.json.tmpl", "unbihex"),
    "xraycl_ubisrv": ("unbihex-server-reality-dns.json.tmpl", "unbihex"),
    "ubicl_xraysrv": ("xray-server-reality-dns.json.tmpl", "xray"),
}
# The newer Unbihex config shape: dns.servers instead of dns.upstream. Old
# builds reject the unknown field, so it is a separate template and opt-in.
# Scenarios where only the client resolves names: the bench cannot prove anything
# by editing the server config, so the client's own state.json is patched instead.
CLIENT_DNS_ONLY = set()


def ub_client_state_path(scenario):
    """Path of the Unbihex CLI state.json used by this scenario, if any."""
    for proc in SCENARIOS[scenario]["start"]:
        if proc["name"] != "client" or "unbihex-cli" not in proc["bin"]:
            continue
        args = proc["args"]
        if "--dir" in args:
            return ROOT / args[args.index("--dir") + 1] / "state.json"
    return None


def patch_client_dns(scenario, upstreams):
    """Point the Unbihex client's own resolver at dnsd before it starts.

    Unbihex resolves target names in the client, so dns.upstream in a server
    config proves nothing: without this the client keeps using the OS resolver.
    """
    path = ub_client_state_path(scenario)
    if path is None or not path.exists():
        return None
    original = path.read_text()
    data = json.loads(original)
    data.setdefault("settings", {})["dns_upstream"] = [f"{host}:{port}"
                                                       for host, port in upstreams]
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path, original


def restore_client_dns(saved):
    if saved is None:
        return
    path, original = saved
    path.write_text(original)


DNS_SERVER_TMPL_SERVERS = {
    "unbihex_reality": ("unbihex-server-reality-dns-servers.json.tmpl", "unbihex-servers"),
    "xraycl_ubisrv": ("unbihex-server-reality-dns-servers.json.tmpl", "unbihex-servers"),
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


def dns_config(scenario, upstreams, dns_servers=False):
    """Render the scenario's server config with dnsd as its only resolver."""
    table = DNS_SERVER_TMPL
    if dns_servers and scenario in DNS_SERVER_TMPL_SERVERS:
        table = DNS_SERVER_TMPL_SERVERS
    tmpl, family = table[scenario]
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
    elif family == "unbihex-servers":
        vals["DNS_SERVERS"] = ", ".join(
            '{"address":"%s","port":%s,"domains":["dns.test"]}'
            % (h.rsplit(":", 1)[0], h.rsplit(":", 1)[1])
            for h in hosts
        )
    else:
        vals["DNS_UPSTREAMS"] = ", ".join(f'"{h}"' for h in hosts)
    out = ROOT / "configs" / tmpl.replace(".tmpl", "")
    render(ROOT / "configs" / tmpl, out, vals)
    return out


def start_dnsd(tag, port, delay_ms=0, fail=False, refuse=False, no_soa=False, zone=None):
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
    if no_soa:
        cmd.append("--no-soa")
    if zone is not None:
        cmd.extend(["--zone", str(zone)])
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


def dns_run(scenario, sub, n, delay_ms, label, rtt, timeout, dns_servers=False,
            no_soa=False):
    if scenario not in SCENARIOS:
        sys.exit(f"unknown scenario {scenario}; known: {', '.join(SCENARIOS)}")
    if scenario not in DNS_SERVER_TMPL and scenario not in CLIENT_DNS_ONLY:
        sys.exit(f"{scenario}: no DNS server template; use one of "
                 f"{', '.join(sorted(set(DNS_SERVER_TMPL) | CLIENT_DNS_ONLY))}")
    if not (ROOT / ".state/keys.env").exists():
        say("no rendered configs yet; calling setup.sh")
        subprocess.run([str(ROOT / "setup.sh")], cwd=ROOT, check=True)
    if sub in ("cold", "warm") and n > 200:
        sys.exit(f"{sub}: the zone has 200 names; use N <= 200")
    RESULTS.mkdir(exist_ok=True)
    ensure_sink6()
    set_rtt(rtt)
    clean = []
    client_state = None
    try:
        if sub == "dead":
            dnsd = [start_dnsd("dead", DNS_BASE_PORT, delay_ms, fail=True, no_soa=no_soa),
                    start_dnsd("live", DNS_DEAD_PORT, no_soa=no_soa)]
            upstreams = [("127.0.0.1", DNS_BASE_PORT), ("127.0.0.1", DNS_DEAD_PORT)]
        else:
            dnsd = [start_dnsd("live", DNS_BASE_PORT, delay_ms, no_soa=no_soa)]
            upstreams = [("127.0.0.1", DNS_BASE_PORT)]
        clean.extend(dnsd)
        client_state = patch_client_dns(scenario, upstreams)
        if scenario in CLIENT_DNS_ONLY:
            # The server resolves nothing this measurement depends on: only the
            # client's own resolver is under test.
            up(scenario)
        else:
            cfg = dns_config(scenario, upstreams, dns_servers)
            dns_up(scenario, cfg)
        port = socks_port(scenario)
        log = dnsd[0]["log"]
        if scenario in CLIENT_DNS_ONLY:
            is_unbihex = True
        else:
            is_unbihex = (DNS_SERVER_TMPL_SERVERS if (dns_servers and scenario in
                          DNS_SERVER_TMPL_SERVERS) else DNS_SERVER_TMPL)[scenario][1] \
                .startswith("unbihex")

        def require_dns_traffic():
            """Old Unbihex ignores dns.upstream without a TUN: refuse, do not fake."""
            if not is_unbihex:
                return
            if sum(sum(log_queries(d["log"]).values()) for d in dnsd) > 0:
                return
            stop(scenario)
            shape = "dns.servers" if dns_servers else "dns.upstream"
            sys.exit(
                f"dns {scenario} {sub}: dnsd received 0 queries even though "
                f"{shape} points at it. This Unbihex build used the system "
                f"resolver (its own DNS resolver is only used with an explicit "
                f"dns.servers/dns.upstream or a TUN), so the measurement is "
                f"invalid and is not reported. See the DNS section of README.md."
            )

        # Like measure(): the first connection(s) through a fresh engine include
        # startup noise. Warm up on a name the measurements do not count.
        if sub != "warm":
            for _ in range(2):
                try:
                    socks_probe(port, "n198.dns.test", timeout)
                except Exception:
                    pass
            require_dns_traffic()

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
                try:
                    socks_probe(port, name, timeout)
                except Exception:
                    pass
                require_dns_traffic()
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
        restore_client_dns(client_state)
        for info in clean:
            stop_dnsd(info["tag"])
        set_rtt(0)



def save_soak_result(scenario, n, label, rtt, dnsd_info, zone, extra):
    result = provenance(scenario, rtt)
    result["kind"] = "dns"
    result["subcommand"] = "soak"
    result["n"] = n
    result["label"] = label
    result["engine"] = SCENARIOS[scenario]["start"][0]["bin"]
    result["dnsd"] = {
        "upstreams": [{"port": u["port"], "mode": u["mode"], "delay_ms": u["delay_ms"]}
                      for u in dnsd_info],
        "zone": str(Path(zone).relative_to(ROOT)),
        "zone_names": len(json.loads(Path(zone).read_text())["names"]),
    }
    result.update(extra)
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"dns-{scenario}-soak-{label}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2))
    say(json.dumps({"scenario": scenario, "sub": "soak", "n": n, "label": label,
                    "cache_size": extra["cache_size"],
                    "disable_cache": extra["disable_cache"],
                    "passes_ms_per_op": extra["passes_ms_per_op"],
                    "rss_delta_peak_kb": extra["rss_kb"]["delta_peak"],
                    "queries": extra["queries"]["total"],
                    "checks": extra["checks"]}, indent=2))
    return path


def soak_run(scenario, n, cache, no_cache, passes, delay_ms, label, rtt):
    """N names, several passes, optional cache knobs, RSS of server and client.

    Answers what the other DNS subcommands cannot: what a full cache costs in
    memory, and what it saves on a repeat lookup when the resolver is not local.
    """
    if scenario != "unbihex_release":
        sys.exit("dns soak: only unbihex_release is supported; it requires an engine "
                 "that answers with dns.cache_size / dns.disable_cache")
    if not (ROOT / ".state/keys.env").exists():
        say("no rendered configs yet; calling setup.sh")
        subprocess.run([str(ROOT / "setup.sh")], cwd=ROOT, check=True)
    RESULTS.mkdir(exist_ok=True)
    ensure_sink6()
    set_rtt(rtt)
    zone = STATE / "soak-zone.json"
    names = rssmem.write_zone(zone, n)
    dnsd = start_dnsd("soak", DNS_BASE_PORT, delay_ms, zone=zone)
    clean = [dnsd]
    try:
        base = json.loads((ROOT / "configs/unbihex-testserver-reality.json").read_text())
        dns_cfg = {"upstream": [f"127.0.0.1:{DNS_BASE_PORT}"], "ttl": 5}
        if cache:
            dns_cfg["cache_size"] = int(cache)
        if no_cache:
            dns_cfg["disable_cache"] = True
        base["dns"] = dns_cfg
        cfg = ROOT / "configs/soak.json"
        cfg.write_text(json.dumps(base, indent=2))

        clone = copy.deepcopy(SCENARIOS[scenario])
        for proc in clone["start"]:
            if proc["name"] == "server":
                args = list(proc["args"])
                args[args.index("-c") + 1] = str(cfg.relative_to(ROOT))
                proc["args"] = args
        SCENARIOS[scenario] = clone

        up(scenario)
        port = socks_port(scenario)
        server_pid = int(pid_file(scenario, "server").read_text())
        client_pid = int(pid_file(scenario, "client").read_text())
        start = {"server": rssmem.rss_kb(server_pid), "client": rssmem.rss_kb(client_pid)}
        sampler = rssmem.Sampler({"server": server_pid, "client": client_pid})
        sampler.start()

        errors = []
        timings = []
        for _ in range(passes):
            t0 = time.time()
            for name in names:
                try:
                    socks_probe(port, name, 20)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc)[:120])
            timings.append(time.time() - t0)
        peak = sampler.finish()
        end = {"server": rssmem.rss_kb(server_pid), "client": rssmem.rss_kb(client_pid)}
        stop(scenario)
        stop_dnsd("soak")
        clean = []
        log = Path(dnsd["log"])
        queries = len(log.read_text().splitlines()) if log.exists() else 0
        per_op = [round(t * 1000 / max(1, n), 2) for t in timings]
        extra = {
            "cache_size": int(cache) if cache else None,
            "disable_cache": no_cache,
            "dns_delay_ms": delay_ms,
            "passes": passes,
            "passes_ms_per_op": per_op,
            "rss_kb": {"start": start, "peak": peak, "end": end,
                       "delta_peak": {k: (peak[k] or 0) - (start[k] or 0) for k in start}},
            "queries": {"received": queries > 0, "total": queries,
                        "stats": dnsd_stats(dnsd["stats"])},
            "errors": len(errors),
            "first_error": errors[0] if errors else None,
            "checks": {"all_connected": not errors,
                       "cache_hit_on_repeat": passes > 1 and queries <= 2 * n},
        }
        return save_soak_result(scenario, n, label, rtt, [dnsd], zone, extra)
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
    soak_rows = []
    for f in (files if files is not None else sorted(RESULTS.glob(pattern))):
        d = json.loads(f.read_text())
        commit = (d.get("git") or {}).get("commit", "")[:8]
        if d.get("kind") == "dns":
            if d.get("subcommand") == "soak":
                rss = d.get("rss_kb") or {}
                delta = (rss.get("delta_peak") or {}).get("server")
                soak_rows.append({
                    "scenario": d.get("scenario", "?"),
                    "n": d.get("n") or 0,
                    "cache": d.get("cache_size"),
                    "disabled": "yes" if d.get("disable_cache") else "no",
                    "delay": d.get("dns_delay_ms") or 0,
                    "passes": " / ".join(f"{v:.2f}"
                                         for v in (d.get("passes_ms_per_op") or [])),
                    "rss": "—" if delta is None else f"{delta / 1024:.1f}",
                    "queries": (d.get("queries") or {}).get("total"),
                    "checks": "ok" if all((d.get("checks") or {}).values())
                              else "FAIL",
                    "label": d.get("label", ""),
                    "file": f.name,
                })
                continue
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
    if not rows and not dns_rows and not soak_rows:
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
    if soak_rows:
        lines = ["| dns soak | names | cache_size | disabled | dnsd delay ms | "
                 "ms/op per pass | RSS server Δ MiB | queries | checks | label |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for r in soak_rows:
            lines.append(f"| {r['scenario']} | {r['n']} | {fmt(r['cache'])} | {r['disabled']} | "
                         f"{r['delay']} | {r['passes']} | {r['rss']} | {fmt(r['queries'])} | "
                         f"{r['checks']} | {r['label']} |")
        blocks.append("\n".join(lines))
    table = "\n\n".join(blocks)
    say(table)
    if markdown:
        out = ROOT / "report.md"
        out.write_text("# Stand results\n\n" + table +
                       "\n\nMeasured on loopback: see README for the caveats.\n")
        say(f"\nwritten {out.relative_to(ROOT)}")


def _spread_pct(m):
    if not isinstance(m, dict) or m.get("error"):
        return None
    try:
        med = float(m.get("median_mbps"))
        mn = float(m.get("min_mbps"))
        mx = float(m.get("max_mbps"))
    except (TypeError, ValueError):
        return None
    if med <= 0:
        return None
    return (mx - mn) / med * 100.0


def _mbps_cell(m):
    if not isinstance(m, dict):
        return "—"
    if m.get("error"):
        return "!"
    if m.get("median_mbps") is None:
        return "—"
    med = m["median_mbps"]
    mn, mx = m.get("min_mbps"), m.get("max_mbps")
    rng = (f"[{mn:.1f}–{mx:.1f}]" if isinstance(mn, (int, float))
           and isinstance(mx, (int, float)) else "[–]")
    n = m.get("runs") or m.get("count") or "—"
    return f"{med:.1f} {rng} {n}"


def _ttfb_cell(m):
    if not isinstance(m, dict):
        return "—"
    if m.get("error"):
        return "!"
    if m.get("median_ms") is None:
        return "—"
    s = f"{m['median_ms']:.2f}"
    p95 = m.get("p95_ms")
    if isinstance(p95, (int, float)):
        s += f" [p95 {p95:.2f}]"
    n = m.get("runs")
    return f"{s} {n}" if n is not None else s


def _conns_cell(m):
    if not isinstance(m, dict):
        return "—"
    if m.get("error"):
        return "!"
    if m.get("conns_per_sec") is None:
        return "—"
    return f"{m['conns_per_sec']:.1f} {m.get('success', '?')}/{m.get('count', '?')}"


def report_stats(pattern="*.json", valid_only=False):
    """Per-result detail table: medians, ranges, spread and the validity flag."""
    rows = []
    for f in sorted(RESULTS.glob(pattern)):
        d = json.loads(f.read_text())
        if d.get("kind") == "dns" or "metrics" not in d:
            continue
        m = d.get("metrics") or {}
        valid = d.get("valid")
        reasons = d.get("invalid_reasons") or []
        if not isinstance(valid, bool):
            valid, reasons = evaluate_validity(m)
        if valid_only and not valid:
            continue
        down1 = m.get("down1") or {}
        spread = _spread_pct(down1)
        flags = ""
        if not valid:
            flags += "!"
        if any((_spread_pct(m.get(p)) or 0) > 20 for p in ("down1", "down8", "up1")):
            flags += "§"
        rows.append({
            "scenario": d.get("scenario", "?"),
            "rtt": d.get("rtt_ms", "?"),
            "profile": (d.get("link") or {}).get("profile") or "—",
            "label": d.get("label", ""),
            "down1": _mbps_cell(down1),
            "down8": _mbps_cell(m.get("down8")),
            "up1": _mbps_cell(m.get("up1")),
            "ttfb": _ttfb_cell(m.get("ttfb")),
            "conns": _conns_cell(m.get("conns")),
            "spread": "—" if spread is None else f"{spread:.1f}",
            "flag": flags or "ok",
        })
    if not rows:
        say("no results" + (" (valid only)" if valid_only else ""))
        return
    lines = ["| scenario | rtt | profile | label | ↓1 med [min–max] n | ↓8 med [min–max] n | "
             "↑1 med [min–max] n | ttfb ms | conn/s ok/count | spread% | flag |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['scenario']} | {r['rtt']} | {r['profile']} | {r['label']} | "
                     f"{r['down1']} | {r['down8']} | {r['up1']} | {r['ttfb']} | {r['conns']} | "
                     f"{r['spread']} | {r['flag']} |")
    say("\n".join(lines))


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


def bench_matrix(scenarios, rtts, quick, runs, control, profile="wan", loss=None, rate=None):
    produced = []
    if control:
        say("== negative control ==")
        up(scenarios[0])
        check_inline(scenarios[0])
    for rtt in rtts:
        for scenario in scenarios:
            say(f"== {scenario}, RTT {rtt} ms ==")
            up(scenario)
            produced.append(measure(scenario, rtt, quick, runs, label="",
                                    profile=profile, loss=loss, rate=rate))
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
    p.add_argument("--profile", choices=["loopback", "wan"], default="wan",
                   help="link profile: loopback (MTU 65536, offloads on, delay only) "
                        "or wan (MTU 1500, offloads off, jitter+loss)")
    p.add_argument("--loss", type=float, default=None,
                   help="packet loss %% override (default: profile value)")
    p.add_argument("--rate", type=float, default=None,
                   help="netem rate cap in Mbit/s, 0 = unlimited (default). "
                        "A rate cap flattens throughput differences by design")
    p = sub.add_parser("ab")
    p.add_argument("a"); p.add_argument("b"); p.add_argument("rtt", type=int, nargs="?", default=0)
    p.add_argument("--rounds", type=int, default=3); p.add_argument("--quick", action="store_true")
    p.add_argument("--profile", choices=["loopback", "wan"], default="wan")
    p.add_argument("--loss", type=float, default=None)
    p.add_argument("--rate", type=float, default=None,
                   help="netem rate cap in Mbit/s, 0 = unlimited; a rate cap flattens throughput differences by design")
    p = sub.add_parser("report"); p.add_argument("pattern", nargs="?", default="*.json")
    p.add_argument("--stats", action="store_true",
                   help="per-result table: medians, ranges, spread and validity flag")
    p.add_argument("--valid-only", action="store_true",
                   help="with --stats: hide results marked invalid")
    p = sub.add_parser("check"); p.add_argument("scenario")
    p = sub.add_parser("dns")
    p.add_argument("scenario")
    p.add_argument("subcommand",
                   choices=["cold", "warm", "stampede", "dead", "leak", "soak"])
    p.add_argument("n", nargs="?", type=int, default=20)
    p.add_argument("--delay-ms", type=int, default=0)
    p.add_argument("--rtt", type=int, default=0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--label", default="")
    p.add_argument("--dns-servers", action="store_true",
                   help="Unbihex: use the dns.servers config shape instead of dns.upstream")
    p.add_argument("--no-soa", action="store_true",
                   help="dnsd: omit the SOA in AUTHORITY for NODATA/NXDOMAIN")
    p.add_argument("--cache", type=int, default=None,
                   help="soak: Unbihex dns.cache_size (default: the engine's own)")
    p.add_argument("--no-cache", action="store_true",
                   help="soak: set dns.disable_cache")
    p.add_argument("--passes", type=int, default=2,
                   help="soak: passes over the same names (2 = cold then warm)")
    p = sub.add_parser("versions"); p.add_argument("--update", action="store_true")
    p = sub.add_parser("table"); p.add_argument("pattern", nargs="?", default="*.json")
    p = sub.add_parser("bench")
    p.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    p.add_argument("--rtt", default="0,50", help="comma separated, e.g. 0,50")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--control", action="store_true", default=True)
    p.add_argument("--no-control", dest="control", action="store_false")
    p.add_argument("--profile", choices=["loopback", "wan"], default="wan")
    p.add_argument("--loss", type=float, default=None)
    p.add_argument("--rate", type=float, default=None,
                   help="netem rate cap in Mbit/s, 0 = unlimited; a rate cap flattens throughput differences by design")
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
        try:
            measure(args.scenario, args.rtt, args.quick, args.runs, args.label,
                    profile=args.profile, loss=args.loss, rate=args.rate)
        finally:
            set_rtt(0)
    elif args.cmd == "ab":
        try:
            for i in range(args.rounds):
                for s in (args.a, args.b):
                    stop(s); up(s)
                    measure(s, args.rtt, args.quick, 1, f"{s}-r{i + 1}",
                            profile=args.profile, loss=args.loss, rate=args.rate)
        finally:
            set_rtt(0)
    elif args.cmd == "report":
        if args.stats:
            report_stats(args.pattern, valid_only=args.valid_only)
        else:
            report(args.pattern)
    elif args.cmd == "check":
        check(args.scenario)
    elif args.cmd == "dns":
        if args.subcommand == "soak":
            soak_run(args.scenario, args.n, args.cache, args.no_cache, args.passes,
                     args.delay_ms, args.label, args.rtt)
        else:
            dns_run(args.scenario, args.subcommand, args.n, args.delay_ms,
                    args.label, args.rtt, args.timeout, args.dns_servers, args.no_soa)
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
        try:
            produced = bench_matrix(scenarios, rtts, args.quick, args.runs, args.control,
                                    profile=args.profile, loss=args.loss, rate=args.rate)
            report(markdown=True, files=produced)
        finally:
            set_rtt(0)


if __name__ == "__main__":
    main()
