#!/usr/bin/env sh
set -eu

echo "== operating system =="
uname -a
test -r /etc/os-release && sed -n '1,8p' /etc/os-release

echo "== runtime availability =="
command -v python3 || true
python3 --version 2>/dev/null || true
command -v systemctl || true
command -v nginx || true
nginx -v 2>&1 || true

echo "== listeners (read-only) =="
ss -ltnp 2>/dev/null || true

echo "== existing Docker services (read-only; no inspect/env output) =="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true

echo "== capacity =="
df -h / /var /opt 2>/dev/null || df -h /
free -h 2>/dev/null || true

echo "No server state was changed by this audit."
