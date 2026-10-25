#!/usr/bin/env bash
# Show the egress proxy's log. Blocked requests appear as TCP_DENIED.
#   scripts/proxy-log.sh            last 50 requests (access log)
#   scripts/proxy-log.sh 200        last 200 requests
#   scripts/proxy-log.sh cache      startup messages and errors (cache log)
#   scripts/proxy-log.sh denied     only blocked requests
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-50}" in
  cache)  docker compose exec -T egress-proxy tail -n 80 /var/log/squid/cache.log ;;
  denied) docker compose exec -T egress-proxy grep TCP_DENIED /var/log/squid/access.log | tail -n 50 || echo "no blocked requests" ;;
  *)      docker compose exec -T egress-proxy tail -n "${1:-50}" /var/log/squid/access.log ;;
esac
