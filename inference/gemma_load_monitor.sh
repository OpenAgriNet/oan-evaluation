#!/usr/bin/env bash
# Observe configured shared gateways; unavailable gateways are not reported as healthy.
set -u
OUT=${1:-data/gemma_load.log}
CAP=${BENCHMARK_LIMIT:-12}
PORTS=${BENCHMARK_MONITOR_PORTS:-18869}
mkdir -p "$(dirname "$OUT")"
while true; do
  total=0; parts=""; flag=""
  for port in $PORTS; do
    active=$(curl -fsS -m 2 "http://127.0.0.1:$port/health" 2>/dev/null |
      python3 -c 'import sys,json;print(json.load(sys.stdin)["active"])' 2>/dev/null) || active=unknown
    parts="$parts $port=$active"
    if [[ $active == unknown ]]; then flag=" GATEWAY_UNAVAILABLE"; else total=$((total + active)); fi
  done
  if ((total > CAP)); then flag="$flag OVER_CAP"; fi
  echo "$(date -u +%FT%TZ) total=$total$parts$flag" >> "$OUT"
  sleep 5
done
