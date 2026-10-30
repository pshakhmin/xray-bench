"""VmRSS sampling and a zone big enough to fill a resolver cache.

Used by `./stand.py dns soak`: memory is invisible in a 200-name run because the
cache never fills, and a warm measurement is invisible if every name is queried
only once.
"""
import json
import threading
import time
from pathlib import Path


def rss_kb(pid):
    """Resident set size of `pid` in KiB, or None when the process is gone."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


class Sampler(threading.Thread):
    """Samples RSS of several pids and keeps the peak of each."""

    def __init__(self, pids, every=0.02):
        super().__init__(daemon=True)
        self.pids = dict(pids)
        self.every = every
        self.peak = {name: 0 for name in self.pids}
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            for name, pid in self.pids.items():
                value = rss_kb(pid)
                if value and value > self.peak[name]:
                    self.peak[name] = value
            time.sleep(self.every)

    def finish(self):
        self._stop.set()
        self.join(timeout=1)
        return self.peak


def write_zone(path, count, suffix="rss.test"):
    """Writes a zone with `count` names (A for all, AAAA for half) and returns them."""
    names = {}
    for i in range(count):
        record = {"A": ["203.0.113.7"]}
        if i % 2 == 0:
            record["AAAA"] = ["::1"]
        names[f"x{i:04d}.{suffix}"] = record
    Path(path).write_text(json.dumps({"ttl": 60, "names": names}))
    return list(names)
