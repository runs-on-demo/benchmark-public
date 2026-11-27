#!/usr/bin/env python3
"""metrics.cachewarm for a cold/warm job: cache state, what was compiled,
sccache or BuildKit cache counters, and the transparent-cache probe."""
import json
import os
import pathlib
import re
import subprocess

b = pathlib.Path(os.environ["RUNNER_TEMP"]) / "bench"


def load(name):
    try:
        return json.loads((b / name).read_text())
    except Exception:
        return None


def count(name, pattern):
    try:
        return len(re.findall(pattern, (b / name).read_text(), re.M))
    except Exception:
        return None


def du(path):
    try:
        return int(subprocess.run(["du", "-sm", path], capture_output=True, text=True).stdout.split()[0])
    except Exception:
        return None


m = load("metrics.json") or {}
cw = {
    "stage": os.environ.get("STAGE"),
    "variant": os.environ.get("VARIANT") or None,
    "cacheKey": os.environ.get("CACHE_KEY"),
    "restore": load("restore.json"),
    "probe": load("probe.json"),
}
if (b / "test.log").exists():
    cw["compiled"] = {n: count(f"{n}.log", r"^\s+Compiling ") for n in ("test", "clippy", "clippy2")}
    cw["checked"] = {n: count(f"{n}.log", r"^\s+Checking ") for n in ("clippy", "clippy2")}
    cw["downloaded"] = count("test.log", r"^\s+Downloaded ")
    cw["targetMiBAfter"] = du("work/target")
s = load("sccache.json")
if s:
    st = s.get("stats") or {}
    def total(v):
        return sum(v.get("counts", {}).values()) if isinstance(v, dict) else v
    cw["sccache"] = {
        "location": s.get("cache_location"),
        "requests": st.get("compile_requests"),
        "hits": total(st.get("cache_hits")),
        "misses": total(st.get("cache_misses")),
        "writes": st.get("cache_writes"),
        "errors": st.get("cache_errors") and total(st.get("cache_errors")),
    }
if (b / "docker-build.log").exists():
    cw["buildkit"] = {
        "steps": count("docker-build.log", r"^#\d+ \[[^\]]+\] "),
        "cached": count("docker-build.log", r"^#\d+ CACHED$"),
        "done": count("docker-build.log", r"^#\d+ DONE "),
    }
m["cachewarm"] = cw
(b / "metrics.json").write_text(json.dumps(m, indent=1))
print(json.dumps({k: v for k, v in cw.items() if k != "probe"}, indent=1))
