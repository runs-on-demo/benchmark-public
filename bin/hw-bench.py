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


# Filesystems that keep their files in memory: a figure measured on one is RAM, not a disk.
RAM_FSTYPES = {"tmpfs", "ramfs"}
# Block devices that live in memory: zram, the brd RAM disk (/dev/ram0), null_blk, persistent memory.
RAM_DEVICE = re.compile(r"^(zram\d+|ram\d+|nullb\d+|pmem\d+)")


def ram_device(source):
    """Whether a mount's source is a RAM block device, or a device mapper /
    md device built only from RAM ones (its /sys/block/<dev>/slaves)."""
    if not source or not source.startswith("/dev/"):
        return False
    name = os.path.basename(os.path.realpath(re.sub(r"\[.*\]$", "", source)))
    if RAM_DEVICE.match(name):
        return True
    slaves = pathlib.Path("/sys/block") / name / "slaves"
    parts = [p.name for p in slaves.iterdir()] if slaves.is_dir() else []
    return bool(parts) and all(RAM_DEVICE.match(p) for p in parts)


def mount_of(path):
    """The mount holding a path: {target, source, fstype, options}, from findmnt."""
    text = sh(["findmnt", "-J", "-T", str(path), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], check=False, timeout=30)
    try:
        fs = json.loads(text)["filesystems"][0]
    except (ValueError, KeyError, IndexError):
        return None
    return {k: fs.get(k) for k in ("target", "source", "fstype", "options")}


def disk_backing(path):
    """What fio's files really sit on: the mount, and for an overlay the mount
    under its upper layer (where writes land). `ram` is true when that is a
    tmpfs or ramfs, or a RAM block device (zram, /dev/ram*, null_blk, pmem)
    under any filesystem: a workspace in RAM, whatever its top mount says."""
    mount = mount_of(path)
    if not mount:
        return None
    out = {"mount": mount}
    lower = mount
    if mount["fstype"] == "overlay":
        upper = re.search(r"(?:^|,)upperdir=([^,]+)", mount.get("options") or "")
        if upper:
            path = upper.group(1)
            out["upperdir"] = path
            found = mount_of(path)
            # The upper layer often sits outside the job's mount namespace (a container's
            # rootfs, a tmpfs detached once the overlay is up): findmnt then answers with
            # some other mount, the overlay itself or "/". Only a mount holding the path counts.
            if found and found != mount and (found["target"] == "/" or path.startswith(found["target"].rstrip("/") + "/")) and found["fstype"] != "overlay":
                lower = out["upper"] = found
            else:
                lower = None
                out["upper"] = None
                # Unresolved: the path's own words are all there is ("/run/starsling-tmpfs.*").
                out["ram"] = bool(re.search(r"tmpfs|ramfs|(^|/)shm(/|$)", path))
                return out
    out["ram"] = lower["fstype"] in RAM_FSTYPES or ram_device(lower.get("source"))
    return out


GIB = 2**30
# The sustained write: 24 GiB, or what the disk holds with SUSTAINED_HEADROOM_GIB
# left (the default volumes of RunsOn and of GitHub's 2 vCPU runners have about
# 11 GiB free). Direct I/O already bypasses the VM's page cache and disk.backing
# names a RAM disk: the size is there to wear out a host-side write-back cache.
# Capped in time so a slow disk does not hold the job.
SUSTAINED_TARGET_GIB = 24
SUSTAINED_MIN_GIB = 4
SUSTAINED_MAX_SECONDS = 300
# Room left on the filesystem after the file, so a job never fills its own disk.
SUSTAINED_HEADROOM_GIB = 4


def mem_total_bytes():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 0


def maybe_ram(backing, usage):
    """A workspace no block device is seen under, about the size of RAM: it may
    be a RAM disk the mount table hides. Writing it full could take the job's
    memory, so the sustained write skips it."""
    if backing is None:
        return usage.total <= 1.1 * mem_total_bytes()
    under = backing.get("upper") if backing.get("upperdir") else backing.get("mount")
    on_device = bool(under and str(under.get("source") or "").startswith("/dev/"))
    return not on_device and usage.total <= 1.1 * mem_total_bytes()


def sustained_write(ram, backing=None):
    """One long sequential write, direct I/O, flushed at the end (end_fsync),
    timed over the whole file. `fits` is 1 when the full target was written.
    A workspace in RAM is not written: a RAM disk filled to the brim takes the
    job's memory with it."""
    target = SUSTAINED_TARGET_GIB * GIB
    usage = shutil.disk_usage(FIO_DIR)
    free = usage.free
    size = min(target, free - SUSTAINED_HEADROOM_GIB * GIB) // 2**20 * 2**20
    out = {"targetGiB": SUSTAINED_TARGET_GIB, "freeGiB": round(free / GIB, 1), "fits": int(size >= target)}
    if ram or size < SUSTAINED_MIN_GIB * GIB:
        return out
    if maybe_ram(backing, usage):
        out["skipped"] = "workspace the size of RAM, on no visible block device"
        return out
    data = json.loads(
        sh(
            [
                "fio", "--name=sustainedWrite", f"--directory={FIO_DIR}", "--ioengine=libaio", "--direct=1",
                "--bs=1M", "--iodepth=32", "--numjobs=1", f"--size={size}", "--rw=write",
                "--fallocate=none", "--refill_buffers=1", "--end_fsync=1",
                f"--runtime={SUSTAINED_MAX_SECONDS}", "--output-format=json",
                "--filename_format=sustainedWrite.$jobnum",
            ],
            timeout=SUSTAINED_MAX_SECONDS + 300,
        )
    )
    job = data["jobs"][0]
    written = job["write"]["io_bytes"]
    # job_runtime is in ms and covers the final fsync: the rate the disk kept up, not the cache's.
    seconds = job.get("job_runtime", 0) / 1000
    out.update(
        writtenGiB=round(written / GIB, 1),
        seconds=round(seconds, 1),
        MiBps=round(written / 2**20 / seconds, 1) if seconds else None,
    )
    return out


def group_disk():
    shutil.rmtree(FIO_DIR, ignore_errors=True)
    FIO_DIR.mkdir(parents=True)
    out = {"directory": str(FIO_DIR)}
    try:
        backing = disk_backing(FIO_DIR)
        if backing:
            out["backing"] = backing
        for test in FIO_TESTS:
            out[test[0]] = fio(*test)
            for f in FIO_DIR.glob("*"):
                f.unlink()
        try:
            out["sustainedWrite"] = sustained_write(bool(backing and backing["ram"]), backing)
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
            print(f"sustained write failed: {e}", flush=True)
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
