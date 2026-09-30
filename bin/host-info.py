#!/usr/bin/env python3
"""Describe the machine a job landed on, as JSON.

Records what the job actually got rather than what the label advertises:
CPU model, cores, memory, where the workspace and Docker data live and what
backs them, EC2 metadata when the runner is on AWS (instance type, AMI,
spot or on-demand), and the RunsOn stack version and image when the runner is
a RunsOn one. Every probe is best-effort; a missing tool or variable leaves the
field null instead of failing the job.
"""
import datetime
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import urllib.request


def run(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def sudo(cmd, timeout=10):
    if shutil.which("sudo") and os.geteuid() != 0:
        return run(["sudo", "-n", *cmd], timeout)
    return run(cmd, timeout)


def cpu_info():
    model = None
    cpuinfo = pathlib.Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    lscpu = {}
    raw = run(["lscpu", "-J"])
    if raw:
        try:
            for field in json.loads(raw).get("lscpu", []):
                lscpu[field["field"].rstrip(":")] = field.get("data")
        except Exception:
            pass
    # arm64 kernels don't expose "model name"; lscpu maps the part id instead
    # (e.g. "Neoverse-V2"), which is the name people search for.
    if not model or model.lower() in ("", "unknown"):
        model = lscpu.get("Model name")
    vendor = lscpu.get("Vendor ID")
    return {
        "model": model,
        "vendor": vendor,
        "logicalCpus": os.cpu_count(),
        "threadsPerCore": _int(lscpu.get("Thread(s) per core")),
        "maxMHz": _float(lscpu.get("CPU max MHz")),
        "virtualization": run(["systemd-detect-virt"]),
        "hypervisor": lscpu.get("Hypervisor vendor"),
    }


def _int(v):
    try:
        return int(v)
    except Exception:
        return None


def _float(v):
    try:
        return float(v)
    except Exception:
        return None


def memory_gib():
    meminfo = pathlib.Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 1048576, 2)
    return None


def memory_facts():
    """Swap and memory settings: why a job does or doesn't survive a memory
    peak (a runner with swap slows down instead of being killed). Never fails."""
    facts = {}
    meminfo = pathlib.Path("/proc/meminfo")
    if meminfo.exists():
        kb = {}
        for line in meminfo.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                kb[parts[0].rstrip(":")] = int(parts[1])
        if "SwapTotal" in kb:
            facts["swapGiB"] = round(kb["SwapTotal"] / 1048576, 2)
        if "MemAvailable" in kb:
            facts["availableGiB"] = round(kb["MemAvailable"] / 1048576, 2)
    swaps = pathlib.Path("/proc/swaps")
    if swaps.exists():
        devices = []
        for line in swaps.read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                kind = "zram" if "zram" in name else parts[1]
                devices.append({"kind": kind, "sizeGiB": round(int(parts[2]) / 1048576, 2)})
        facts["swapDevices"] = devices
    for key, path in (("overcommit", "/proc/sys/vm/overcommit_memory"), ("swappiness", "/proc/sys/vm/swappiness")):
        f = pathlib.Path(path)
        if f.exists():
            try:
                facts[key] = int(f.read_text().strip())
            except ValueError:
                pass
    zram = sorted(p.name for p in pathlib.Path("/sys/block").glob("zram*")) if pathlib.Path("/sys/block").exists() else []
    if zram:
        facts["zram"] = zram
    return facts or None


def mount_for(path):
    if not path or not os.path.exists(path):
        return None
    raw = run(["findmnt", "-J", "-T", path, "-o", "SOURCE,TARGET,FSTYPE,OPTIONS,SIZE,AVAIL"])
    info = None
    if raw:
        try:
            info = json.loads(raw)["filesystems"][0]
        except Exception:
            info = None
    usage = shutil.disk_usage(path)
    return {
        "path": path,
        "source": info.get("source") if info else None,
        "target": info.get("target") if info else None,
        "fstype": info.get("fstype") if info else None,
        "options": info.get("options") if info else None,
        "sizeGiB": round(usage.total / 2**30, 1),
        "freeGiB": round(usage.free / 2**30, 1),
    }


def block_devices():
    raw = run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,ROTA,TRAN,FSTYPE,MOUNTPOINT"])
    if not raw:
        return None
    try:
        devices = json.loads(raw).get("blockdevices", [])
    except Exception:
        return None

    def slim(d):
        out = {
            "name": d.get("name"),
            "type": d.get("type"),
            "sizeGiB": round(int(d["size"]) / 2**30, 1) if d.get("size") else None,
            "model": (d.get("model") or "").strip() or None,
            "rotational": d.get("rota") in (True, "1", 1),
            "transport": d.get("tran"),
            "fstype": d.get("fstype"),
            "mountpoint": d.get("mountpoint"),
        }
        children = [slim(c) for c in d.get("children", []) or []]
        if children:
            out["children"] = children
        return out

    return [slim(d) for d in devices if d.get("type") in ("disk", "raid0", "lvm", "md")]


def docker_root():
    root = run(["docker", "info", "-f", "{{.DockerRootDir}}"])
    driver = run(["docker", "info", "-f", "{{.Driver}}"])
    return root, driver


def imds():
    """EC2 instance metadata (IMDSv2). Returns None off AWS."""
    base = "http://169.254.169.254/latest"
    try:
        req = urllib.request.Request(
            f"{base}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(req, timeout=1).read().decode()
    except Exception:
        return None

    def get(path):
        try:
            req = urllib.request.Request(
                f"{base}/meta-data/{path}", headers={"X-aws-ec2-metadata-token": token}
            )
            return urllib.request.urlopen(req, timeout=1).read().decode().strip()
        except Exception:
            return None

    return {
        "instanceType": get("instance-type"),
        "lifecycle": get("instance-life-cycle"),
        "amiId": get("ami-id"),
        "availabilityZone": get("placement/availability-zone"),
        "region": get("placement/region"),
    }


# RunsOn exports these to every job. Only facts about the stack and the
# machine are kept: no bucket names, instance ids or anything credential-like.
RUNS_ON_FIELDS = {
    "RUNS_ON_VERSION": "version",
    "RUNS_ON_STACK_NAME": "stackName",
    "RUNS_ON_AMI_ID": "amiId",
    "RUNS_ON_AMI_NAME": "amiName",
    "RUNS_ON_IMAGE_NAME": "imageName",
    "RUNS_ON_IMAGE_ID": "imageId",
    "RUNS_ON_INSTANCE_TYPE": "instanceType",
    "RUNS_ON_INSTANCE_LIFECYCLE": "lifecycle",
    "RUNS_ON_AWS_REGION": "region",
    "RUNS_ON_AWS_AZ": "availabilityZone",
    "RUNS_ON_AGENT_ARCH": "agentArch",
}


def runs_on():
    """The RunsOn stack a job ran on, from the RUNS_ON_* variables the stack
    sets. None on other providers."""
    values = {key: os.environ.get(var) for var, key in RUNS_ON_FIELDS.items() if os.environ.get(var)}
    return values or None


def network_org():
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=4) as resp:
            data = json.loads(resp.read().decode())
        return {
            "org": data.get("org"),
            "city": data.get("city"),
            "region": data.get("region"),
            "country": data.get("country"),
        }
    except Exception:
        return None


def os_release():
    path = pathlib.Path("/etc/os-release")
    if not path.exists():
        return None
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v.strip('"')
    return values.get("PRETTY_NAME")


def main():
    workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    docker_dir, docker_driver = docker_root()
    info = {
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "os": os_release(),
        "cpu": cpu_info(),
        "memoryGiB": memory_gib(),
        "memory": memory_facts(),
        "manufacturer": sudo(["dmidecode", "-s", "system-manufacturer"]),
        "product": sudo(["dmidecode", "-s", "system-product-name"]),
        "storage": {
            "workspace": mount_for(workspace),
            "tmp": mount_for("/tmp"),
            "root": mount_for("/"),
            "docker": mount_for(docker_dir) if docker_dir else None,
            "dockerDriver": docker_driver,
            "devices": block_devices(),
        },
        "ec2": imds(),
        "runsOn": runs_on(),
        "network": network_org(),
        "runner": {
            "name": os.environ.get("RUNNER_NAME"),
            "environment": os.environ.get("RUNNER_ENVIRONMENT"),
            "imageOS": os.environ.get("ImageOS"),
            "imageVersion": os.environ.get("ImageVersion"),
        },
    }
    json.dump(info, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
