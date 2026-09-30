#!/usr/bin/env python3
"""Hardware micro-benchmarks, one group per invocation.

Usage: bin/hw-bench.py <cpu|passmark|memory|disk|network|docker|postgres> --out metrics.json

Each group merges its numbers into the metrics file so a failing group never
loses the others. Units are in the key names (MiBps, iops, us, seconds, tps).
Disk tests run in the job workspace, because that is where builds write.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

WORKSPACE = pathlib.Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())


def sh(cmd, timeout=900, check=True, **kw):
    print("+", cmd if isinstance(cmd, str) else " ".join(cmd), flush=True)
    res = subprocess.run(
        cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, **kw
    )
    sys.stdout.write(res.stdout[-4000:])
    sys.stderr.write(res.stderr[-2000:])
    if check and res.returncode != 0:
        raise RuntimeError(f"command failed ({res.returncode}): {cmd}")
    return res.stdout


def nproc():
    return os.cpu_count() or 1


# ── CPU ──────────────────────────────────────────────────────────────────


def group_cpu():
    out = {}
    for threads, key in ((1, "sysbenchSingleEps"), (nproc(), "sysbenchMultiEps")):
        text = sh(["sysbench", "cpu", f"--threads={threads}", "--time=20", "run"])
        m = re.search(r"events per second:\s+([\d.]+)", text)
        out[key] = float(m.group(1)) if m else None
    text = sh(["7z", "b"], timeout=600)
    m = re.search(r"^Tot:\s+\S+\s+\S+\s+(\d+)", text, re.M)
    out["sevenZipMips"] = int(m.group(1)) if m else None
    return {"cpu": out}


def group_passmark():
    binary = pathlib.Path("/tmp/PerformanceTest/pt_linux")
    if not binary.exists():
        sh(str(pathlib.Path(__file__).parent / "install-passmark.sh"), timeout=600)
    workdir = pathlib.Path("/tmp/passmark-run")
    workdir.mkdir(exist_ok=True)
    sh(
        f'TERM=xterm script -q -c "{binary} -r 1 -i 2 -p {nproc()} -d 2" /dev/null',
        timeout=900,
        cwd=workdir,
    )
    results = workdir / "results_cpu.yml"
    text = results.read_text() if results.exists() else ""
    single = re.search(r"CPU_SINGLETHREAD:\s*([\d.]+)", text)
    mark = re.search(r"SUMM_CPU:\s*([\d.]+)", text)
    return {
        "cpu": {
            "passmarkSingle": float(single.group(1)) if single else None,
            "passmarkCpuMark": float(mark.group(1)) if mark else None,
        }
    }


# ── Memory ───────────────────────────────────────────────────────────────


def group_memory():
    out = {}
    for oper, key in (("read", "readMiBps"), ("write", "writeMiBps")):
        text = sh(
            ["sysbench", "memory", f"--memory-oper={oper}", f"--threads={nproc()}", "--time=15", "run"]
        )
        m = re.search(r"\(([\d.]+) MiB/sec\)", text)
        out[key] = float(m.group(1)) if m else None
    return {"memory": out}


# ── Disk ─────────────────────────────────────────────────────────────────

FIO_DIR = WORKSPACE / ".fio"

# name, rw, block size, iodepth, jobs, extra args
FIO_TESTS = [
    ("seqRead", "read", "1M", 32, 1, []),
    ("seqWrite", "write", "1M", 32, 1, []),
    ("randRead", "randread", "4k", 32, 4, []),
    ("randWrite", "randwrite", "4k", 32, 4, []),
    # One outstanding 4K read at a time: what small-file heavy steps
    # (npm/pnpm/bun install, cargo metadata, git checkout) actually wait on.
    ("randReadQd1", "randread", "4k", 1, 1, []),
    # 4K writes each followed by fdatasync, like a database commit log.
    ("syncWrite", "write", "4k", 1, 1, ["--fdatasync=1"]),
]


def fio(name, rw, bs, iodepth, jobs, extra, runtime=20):
    reads = "read" in rw
    # 2 GiB per job, 1 GiB when several jobs run, so small disks still fit.
    size = "2G" if jobs == 1 else "1G"
    if reads:
        # Lay real data down first so reads never hit sparse extents.
        sh(
            [
                "fio", f"--name={name}-prep", f"--directory={FIO_DIR}", "--ioengine=libaio",
                "--direct=1", "--bs=1M", "--iodepth=32", f"--size={size}", f"--numjobs={jobs}",
                "--rw=write", "--fallocate=none", "--refill_buffers=1", "--end_fsync=1",
                f"--filename_format={name}.$jobnum",
            ],
            timeout=900,
        )
    cmd = [
        "fio", f"--name={name}", f"--directory={FIO_DIR}", "--ioengine=libaio", "--direct=1",
        f"--bs={bs}", f"--iodepth={iodepth}", f"--numjobs={jobs}", f"--size={size}",
        f"--rw={rw}", "--time_based", f"--runtime={runtime}", "--ramp_time=2s",
        "--group_reporting=1", "--output-format=json", f"--filename_format={name}.$jobnum",
        *extra,
    ]
    if reads:
        cmd += ["--allow_file_create=0", "--readonly"]
    data = json.loads(sh(cmd, timeout=900))
    job = data["jobs"][0]
    side = job["read"] if reads else job["write"]
    lat = side.get("clat_ns", {}).get("percentile", {})
    return {
        "MiBps": round(side["bw_bytes"] / 2**20, 1),
        "iops": round(side["iops"]),
        "p99us": round(lat.get("99.000000", 0) / 1000, 1) if lat else None,
    }


def group_disk():
    shutil.rmtree(FIO_DIR, ignore_errors=True)
    FIO_DIR.mkdir(parents=True)
    out = {"directory": str(FIO_DIR)}
    try:
        for test in FIO_TESTS:
            out[test[0]] = fio(*test)
            for f in FIO_DIR.glob("*"):
                f.unlink()
    finally:
        shutil.rmtree(FIO_DIR, ignore_errors=True)
    return {"disk": out}


# ── Network / Docker ─────────────────────────────────────────────────────


def timed_download(url, max_seconds=60):
    text = sh(
        ["curl", "-sS", "-L", "-o", "/dev/null", "--max-time", str(max_seconds),
         "-w", "%{speed_download} %{size_download} %{time_total}", url],
        timeout=max_seconds + 30,
        check=False,
    )
    try:
        speed, size, total = text.split()
        return {"MiBps": round(float(speed) / 2**20, 1), "MiB": round(float(size) / 2**20, 1),
                "seconds": round(float(total), 2)}
    except ValueError:
        return None


def group_network():
    return {
        "network": {
            # 134 MiB kernel tarball from kernel.org's CDN (Fastly).
            "cdnDownload": timed_download("https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.tar.xz"),
            # Same host actions/checkout, release downloads and ghcr pulls go through.
            "githubDownload": timed_download(
                "https://github.com/oven-sh/bun/releases/download/bun-v1.3.14/bun-linux-x64.zip"
            ),
        }
    }


def group_docker():
    image = os.environ.get("BENCH_DOCKER_IMAGE", "node:24-bookworm")
    sh(["docker", "rmi", "-f", image], check=False)
    sh(["docker", "builder", "prune", "-af"], check=False)
    start = time.monotonic()
    sh(["docker", "pull", "-q", image], timeout=900)
    pull = time.monotonic() - start
    start = time.monotonic()
    sh(["docker", "run", "--rm", image, "node", "-e", "1"], timeout=300)
    first_run = time.monotonic() - start
    return {"docker": {"image": image, "pullSeconds": round(pull, 2), "firstRunSeconds": round(first_run, 2)}}


# ── PostgreSQL ───────────────────────────────────────────────────────────


def group_postgres():
    # Started here rather than as a job service container, so a runner
    # without service-container support still reports everything else.
    sh(["docker", "rm", "-f", "bench-pg"], check=False)
    sh(["docker", "run", "-d", "--name", "bench-pg", "-e", "POSTGRES_PASSWORD=postgres",
        "-e", "POSTGRES_DB=bench", "-p", "5432:5432", "postgres:16"], timeout=600)
    for _ in range(60):
        if subprocess.run(["pg_isready", "-h", "localhost", "-p", "5432"], capture_output=True).returncode == 0:
            break
        time.sleep(1)
    time.sleep(2)
    env = dict(os.environ, PGPASSWORD="postgres")
    base = ["-h", "localhost", "-U", "postgres"]
    sh(["pgbench", "-i", "-s", "50", *base, "bench"], env=env, timeout=900)
    out = {}
    for key, flags in (("readOnlyTps", ["-S"]), ("readWriteTps", [])):
        text = sh(["pgbench", *base, *flags, "-c", "10", "-j", str(nproc()), "-T", "30", "bench"],
                  env=env, timeout=300)
        m = re.search(r"tps = ([\d.]+)", text)
        out[key] = float(m.group(1)) if m else None
    return {"postgres": out}


GROUPS = {
    "cpu": group_cpu,
    "passmark": group_passmark,
    "memory": group_memory,
    "disk": group_disk,
    "network": group_network,
    "docker": group_docker,
    "postgres": group_postgres,
}


def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=sorted(GROUPS))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    path = pathlib.Path(args.out)
    metrics = json.loads(path.read_text()) if path.exists() else {}
    started = time.monotonic()
    try:
        merge(metrics, GROUPS[args.group]())
        status = 0
    except Exception as exc:  # keep the other groups' numbers
        print(f"::warning::{args.group} failed: {exc}", flush=True)
        metrics.setdefault("errors", {})[args.group] = str(exc)[:500]
        status = 1
    metrics.setdefault("groupSeconds", {})[args.group] = round(time.monotonic() - started, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics.get(args.group, metrics), indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
