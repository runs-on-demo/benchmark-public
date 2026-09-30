#!/usr/bin/env bash
# Install PassMark PerformanceTest for Linux from passmark.com.
# The binary links against libncurses.so.5, which Ubuntu 24.04 no longer
# ships, so it is pulled from the focal-security archive when missing.
set -euo pipefail

case "$(uname -m)" in
  x86_64) ASSET=PerformanceTest_Linux_x86-64; PORTS=http://security.ubuntu.com/ubuntu ;;
  aarch64|arm64) ASSET=PerformanceTest_Linux_ARM64; PORTS=http://ports.ubuntu.com/ubuntu-ports ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if ! ldconfig -p | grep -q 'libncurses.so.5'; then
  echo "deb $PORTS focal-security main universe" | sudo tee /etc/apt/sources.list.d/focal-security.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libncurses5 libtinfo5
fi

tmp=$(mktemp -d)
# passmark.com rejects curl's default user agent.
curl -fsSL -A "Mozilla/5.0 (X11; Linux $(uname -m))" -o "$tmp/pt.zip" \
  "https://www.passmark.com/downloads/${ASSET}.zip"
unzip -q -o "$tmp/pt.zip" -d "$tmp"
mkdir -p /tmp/PerformanceTest
install -m 0755 "$tmp/PerformanceTest/${ASSET}" /tmp/PerformanceTest/pt_linux
rm -rf "$tmp"
/tmp/PerformanceTest/pt_linux --help >/dev/null 2>&1 || true
