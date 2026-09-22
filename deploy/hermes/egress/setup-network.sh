#!/usr/bin/env bash
# Create the egress-filtered Docker network for ATLAS agent sandboxes (H0 gap G3).
#
#   atlas-agents      internal network, no route out; every sandbox joins it
#                     (terminal.docker_extra_args in deploy/hermes/managed/config.yaml)
#   atlas-egress-out  ordinary bridge used only by the proxy
#   atlas-egress      squid, on both networks, allowing only squid.conf's hosts
#
# Idempotent. Run as a user that can talk to the Docker daemon. Pin the image
# to a digest after the first pull: SQUID_IMAGE=ubuntu/squid@sha256:... ./setup-network.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SQUID_IMAGE="${SQUID_IMAGE:-ubuntu/squid:latest}"

docker network inspect atlas-agents >/dev/null 2>&1 \
  || docker network create --internal atlas-agents
docker network inspect atlas-egress-out >/dev/null 2>&1 \
  || docker network create atlas-egress-out

docker rm -f atlas-egress >/dev/null 2>&1 || true
docker run -d --name atlas-egress --restart unless-stopped \
  --network atlas-egress-out \
  -v "$HERE/squid.conf:/etc/squid/squid.conf:ro" \
  "$SQUID_IMAGE" >/dev/null
docker network connect atlas-agents atlas-egress

echo "atlas-agents network ready; proxy atlas-egress allows:"
grep -E '^acl allowed' "$HERE/squid.conf" | sed 's/^acl allowed dstdomain /  /'
echo "Verify with: $HERE/verify-egress.sh"
