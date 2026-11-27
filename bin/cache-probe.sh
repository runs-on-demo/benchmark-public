#!/usr/bin/env bash
# Records, before the workload runs, every cache the runner could apply
# without the workflow asking for it (package proxies, compiler caches,
# Docker builders and mirrors, leftovers on disk). Writes JSON to $1.
# Values that look like secrets are never printed: only variable names, hosts
# and config keys.
out="$1"
py() { python3 - "$@"; }

envnames=$(env | cut -d= -f1 | grep -E '^(CARGO_|RUSTC_WRAPPER|RUSTC|SCCACHE_|CCACHE_|GOCACHEPROG|GOPROXY|GOFLAGS|npm_config_|NPM_CONFIG_|BUN_|YARN_|PIP_|UV_|TURBO_|NX_|BAZEL|GRADLE|DOCKER_|BUILDX_|BUILDKIT|ACTIONS_CACHE|ACTIONS_RESULTS_URL|ACTIONS_RUNTIME_URL|http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY|no_proxy|NO_PROXY)' | sort | tr '\n' ' ')

# Variables whose value is a URL or a switch: keep only scheme+host (or the
# word), never paths with tokens.
envhosts=$(for n in RUSTC_WRAPPER SCCACHE_WEBDAV_ENDPOINT SCCACHE_BUCKET SCCACHE_GHA_ENABLED npm_config_registry NPM_CONFIG_REGISTRY GOPROXY ACTIONS_CACHE_URL ACTIONS_RESULTS_URL ACTIONS_CACHE_SERVICE_V2 DOCKER_HOST BUILDX_BUILDER http_proxy https_proxy HTTP_PROXY HTTPS_PROXY TURBO_API TURBO_CACHE; do
  v="${!n:-}"; [ -n "$v" ] || continue
  printf '%s=%s ' "$n" "$(printf '%s' "$v" | sed -E 's#^([a-z+]+://[^/]+).*#\1#')"
done)

cargo_home="${CARGO_HOME:-$HOME/.cargo}"
cargo_cfg=""
for f in "$cargo_home/config.toml" "$cargo_home/config" /etc/cargo/config.toml; do
  [ -f "$f" ] && cargo_cfg+="$f: $(grep -E '^\s*(\[|replace-with|registry|index|rustc-wrapper|target-dir)' "$f" | sed -E 's#(https?://[^/" ]+)[^" ]*#\1#g' | tr '\n' ';') "
done
apt_proxy=$(grep -rhsE 'Acquire::(http|https)::Proxy|^deb .*://' /etc/apt/apt.conf /etc/apt/apt.conf.d/ /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | sed -E 's#(https?://[^/" ]+)[^" ]*#\1#g' | sort -u | head -8 | tr '\n' ';')
apt_sources_hosts=$(grep -rhsoE 'https?://[^/ ]+' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | sort -u | tr '\n' ' ')
daemon_json=$(sudo -n cat /etc/docker/daemon.json 2>/dev/null | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in d if k in ("registry-mirrors","features","builder","data-root","storage-driver","insecure-registries")}))
except Exception: print("")')
buildx_ls=$(docker buildx ls 2>/dev/null | head -12 | tr -s ' ' | tr '\n' ';')
docker_ctx=$(docker context ls --format '{{.Name}}={{.DockerEndpoint}}{{if .Current}}*{{end}}' 2>/dev/null | tr '\n' ' ')
docker_images=$(docker images -q 2>/dev/null | wc -l)
buildkit_volumes=$(docker volume ls -q 2>/dev/null | grep -c buildx || true)
hosts_extra=$(grep -vE '^\s*(#|$)|localhost|ip6-' /etc/hosts 2>/dev/null | awk '{ $1=""; print }' | tr '\n' ';')
du_mb() { [ -e "$1" ] && du -sm "$1" 2>/dev/null | cut -f1 || echo 0; }

py "$out" <<PY
import json, sys
json.dump({
  "envNames": "$envnames".split(),
  "envHosts": "$envhosts".split(),
  "cargoConfig": """$cargo_cfg""".strip(),
  "aptProxy": """$apt_proxy""".strip(),
  "aptSourceHosts": "$apt_sources_hosts".split(),
  "dockerDaemon": """$daemon_json""".strip(),
  "buildx": """$buildx_ls""".strip(),
  "dockerContexts": "$docker_ctx".strip(),
  "dockerImages": int("$docker_images" or 0),
  "buildxVolumes": int("$buildkit_volumes" or 0),
  "hostsOverrides": """$hosts_extra""".strip(),
  "preexistingMiB": {
    "cargoRegistry": int("$(du_mb "$cargo_home/registry")"),
    "cargoGit": int("$(du_mb "$cargo_home/git")"),
    "sccacheLocal": int("$(du_mb "$HOME/.cache/sccache")"),
    "workspaceTarget": int("$(du_mb "$GITHUB_WORKSPACE/work/target")"),
  },
}, open(sys.argv[1], "w"), indent=1)
PY
cat "$out"
