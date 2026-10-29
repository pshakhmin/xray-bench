#!/usr/bin/env python3
"""Reproducible performance stand for proxy engines.

Subcommands:
  up <scenario>                 start the scenario's server and client
  down [<scenario>|--all]       stop processes of one scenario (or all)
  run <scenario> <rtt_ms>       measure; --quick, --runs N, --label TEXT
  ab <a> <b> <rtt_ms>           alternate two scenarios; --rounds N
  report [<glob>]               markdown table from results/*.json
  check <scenario>              negative controls: the tunnel must be used

Everything the result depends on is recorded in the result file: binary
hashes, versions, kernel, sysctls, netem parameters, git commit.
"""
import argparse
import glob
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
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


def up(scenario):
    if scenario not in SCENARIOS:
        sys.exit(f"unknown scenario {scenario}; known: {', '.join(SCENARIOS)}")
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


def report(pattern="*.json", markdown=False, files=None):
    rows = []
    for f in (files if files is not None else sorted(RESULTS.glob(pattern))):
        d = json.loads(f.read_text())
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
            "commit": (d.get("git") or {}).get("commit", "")[:8],
            "file": f.name,
        })
    if not rows:
        sys.exit("no results yet")
    lines = []
    lines.append("| scenario | rtt | label | ↓1 MB/s | ↓8 MB/s | ↑1 MB/s | TTFB ms | conn/s | srv ms/MiB | commit |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        fmt = lambda v: "—" if v is None else f"{v:.1f}" if isinstance(v, float) else str(v)
        lines.append(f"| {r['scenario']} | {r['rtt']} | {r['label']} | {fmt(r['down1'])} | {fmt(r['down8'])} | "
                     f"{fmt(r['up1'])} | {fmt(r['ttfb'])} | {fmt(r['conns'])} | {fmt(r['srv_cpu'])} | {r['commit']} |")
    table = "\n".join(lines)
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
