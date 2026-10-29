#!/usr/bin/env python3
"""Check and update the pinned upstream release binaries in versions.env.

    python3 tools/pins.py --check    # report pinned vs latest, exit 0
    python3 tools/pins.py --update   # rewrite versions.env in place

Only the stdlib is used. Set GH_TOKEN or GITHUB_TOKEN to raise GitHub API limits.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "versions.env"

XRAY_REPO = "XTLS/Xray-core"
SINGBOX_REPO = "SagerNet/sing-box"
UNBIHEX_REPO = "crystalmasta/unbihex"

XRAY_ASSET = "Xray-linux-64.zip"

SUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$")
CLI_ASSET_RE = re.compile(r"^unbihex-cli-([0-9][^-]*)-linux-x86_64\.tar\.gz$")
SERVER_ASSET_RE = re.compile(r"^test_server-([0-9][^-]*)-linux-x86_64\.tar\.gz$")


def headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "xray-bench-pins"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_tag(repo):
    """Return the newest release tag without the leading 'v'."""
    data = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))
    return data["tag_name"].lstrip("v")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_env():
    vals = {}
    for line in VERSIONS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        vals[key] = value
    return vals


def write_env(changes):
    """Rewrite KEY=value lines in place, preserving comments, order and layout."""
    lines = VERSIONS.read_text().splitlines(keepends=True)
    out = []
    seen = set()
    changed = False
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m and m.group(1) in changes:
            key = m.group(1)
            seen.add(key)
            new = f"{key}={changes[key]}\n"
            if line != new:
                changed = True
            out.append(new)
        else:
            out.append(line)
    for key, value in changes.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
            changed = True
    if changed:
        VERSIONS.write_text("".join(out))
    return changed


def unbihex_assets(tag):
    """Return (cli_name, cli_sha, server_name, server_sha, server_version)."""
    url = f"https://github.com/{UNBIHEX_REPO}/releases/download/v{tag}/SHA256SUMS.txt"
    sums = {}
    for line in get(url).decode().splitlines():
        m = SUM_RE.match(line.strip())
        if m:
            sums[m.group(2)] = m.group(1)
    cli = server = None
    for name in sums:
        if CLI_ASSET_RE.match(name):
            cli = name
        elif SERVER_ASSET_RE.match(name):
            server = name
    if not cli:
        raise RuntimeError(f"no unbihex-cli linux-x86_64 asset in v{tag} SHA256SUMS.txt")
    if not server:
        raise RuntimeError(f"no test_server linux-x86_64 asset in v{tag} SHA256SUMS.txt")
    server_version = SERVER_ASSET_RE.match(server).group(1)
    return cli, sums[cli], server, sums[server], server_version


def state(pinned, latest):
    if latest.startswith("unavailable"):
        return "unavailable"
    return "up to date" if pinned == latest else "NEW"


def collect():
    """Return the report rows and the raw latest values needed by --update."""
    rows = []
    latest = {}

    try:
        tag = latest_tag(XRAY_REPO)
        latest["xray"] = tag
    except Exception as e:  # noqa: BLE001 - offline/rate limit is reported, not fatal
        tag = f"unavailable ({type(e).__name__})"
    rows.append(("xray", tag, XRAY_REPO))

    try:
        tag = latest_tag(SINGBOX_REPO)
        latest["singbox"] = tag
    except Exception as e:  # noqa: BLE001
        tag = f"unavailable ({type(e).__name__})"
    rows.append(("sing-box", tag, SINGBOX_REPO))

    try:
        tag = latest_tag(UNBIHEX_REPO)
        cli, cli_sha, server, server_sha, server_version = unbihex_assets(tag)
        latest["unbihex"] = {
            "tag": tag,
            "cli": cli,
            "cli_sha": cli_sha,
            "server": server,
            "server_sha": server_sha,
            "server_version": server_version,
        }
    except Exception as e:  # noqa: BLE001
        tag = f"unavailable ({type(e).__name__})"
        server_version = tag
    rows.append(("unbihex-cli", tag, UNBIHEX_REPO))
    rows.append(("test_server", server_version, UNBIHEX_REPO))
    return rows, latest


def check():
    env = read_env()
    rows, _ = collect()
    report = []
    for asset, latest, _repo in rows:
        if asset == "xray":
            pinned = env.get("XRAY_VERSION", "?").lstrip("v")
        elif asset == "sing-box":
            pinned = env.get("SINGBOX_VERSION", "?")
        elif asset == "unbihex-cli":
            pinned = env.get("UNBIHEX_VERSION", "?").lstrip("v")
        else:
            pinned = env.get("UNBIHEX_SERVER_ASSET_VERSION", "?")
        report.append((asset, pinned, latest, state(pinned, latest)))

    print("asset | pinned | latest | state")
    print("--- | --- | --- | ---")
    for asset, pinned, latest, st in report:
        print(f"{asset} | {pinned} | {latest} | {st}")
    return 0


def update():
    env = read_env()
    _, latest = collect()

    missing = [k for k in ("xray", "singbox", "unbihex") if k not in latest]
    if missing:
        print(f"error: latest release unavailable for: {', '.join(missing)}", file=sys.stderr)
        return 1

    changes = {}

    if env.get("XRAY_VERSION", "").lstrip("v") != latest["xray"]:
        url = f"https://github.com/{XRAY_REPO}/releases/download/v{latest['xray']}/{XRAY_ASSET}"
        digest = sha256_bytes(get(url, timeout=300))
        changes["XRAY_VERSION"] = f"v{latest['xray']}"
        changes["XRAY_ZIP"] = XRAY_ASSET
        changes["XRAY_URL"] = (
            f"https://github.com/{XRAY_REPO}/releases/download/${{XRAY_VERSION}}/${{XRAY_ZIP}}"
        )
        changes["XRAY_SHA256"] = digest

    if env.get("SINGBOX_VERSION", "") != latest["singbox"]:
        ver = latest["singbox"]
        asset = f"sing-box-{ver}-linux-amd64.tar.gz"
        url = f"https://github.com/{SINGBOX_REPO}/releases/download/v{ver}/{asset}"
        digest = sha256_bytes(get(url, timeout=300))
        changes["SINGBOX_VERSION"] = ver
        changes["SINGBOX_TGZ"] = asset
        changes["SINGBOX_URL"] = (
            f"https://github.com/{SINGBOX_REPO}/releases/download/v${{SINGBOX_VERSION}}/${{SINGBOX_TGZ}}"
        )
        changes["SINGBOX_SHA256"] = digest

    ubi = latest["unbihex"]
    base = f"https://github.com/{UNBIHEX_REPO}/releases/download/${{UNBIHEX_VERSION}}"
    desired = {
        "UNBIHEX_VERSION": f"v{ubi['tag']}",
        "UNBIHEX_CLI_TGZ": ubi["cli"],
        "UNBIHEX_CLI_URL": f"{base}/${{UNBIHEX_CLI_TGZ}}",
        "UNBIHEX_CLI_SHA256": ubi["cli_sha"],
        "UNBIHEX_SERVER_ASSET_VERSION": ubi["server_version"],
        "UNBIHEX_SERVER_TGZ": ubi["server"],
        "UNBIHEX_SERVER_URL": f"{base}/${{UNBIHEX_SERVER_TGZ}}",
        "UNBIHEX_SERVER_SHA256": ubi["server_sha"],
    }
    for key, value in desired.items():
        if env.get(key) != value:
            changes[key] = value

    if not changes:
        print("all pins up to date; nothing changed")
        return 0

    write_env(changes)
    for key in changes:
        print(f"updated {key}")
    print("pins updated; setup.sh re-downloads binaries whose version no longer matches")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report pinned vs latest")
    mode.add_argument("--update", action="store_true", help="rewrite versions.env")
    args = parser.parse_args(argv)
    if args.update:
        return update()
    return check()


if __name__ == "__main__":
    sys.exit(main())
