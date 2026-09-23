#!/usr/bin/env bash
# Start/stop/status for background loaders without ever putting the process pattern on a
# command line that also launches it (which would make pkill kill the launching shell).
# Usage: scripts/bg.sh {start|stop|status} {kalshi_markets|kalshi_trades|poly_markets|poly_trades} [extra args]
set -u
cd "$(dirname "$0")/.."
action="${1:-status}"
name="${2:-}"
case "$name" in
  kalshi_markets) pat='download_kalshi.py --phase markets'; cmd=".venv/bin/python scripts/download_kalshi.py --phase markets --rps 2"; log=data/checkpoints/kalshi/log_markets.txt;;
  kalshi_trades)  pat='download_kalshi.py --phase trades';  cmd=".venv/bin/python scripts/download_kalshi.py --phase trades --rps 2";  log=data/checkpoints/kalshi/log_trades.txt;;
  kalshi_candles) pat='download_kalshi.py --phase candles'; cmd=".venv/bin/python scripts/download_kalshi.py --phase candles --rps 2"; log=data/checkpoints/kalshi/log_candles.txt;;
  poly_markets)   pat='download_polymarket.py --phase markets'; cmd=".venv/bin/python scripts/download_polymarket.py --phase markets --start 2024-01-01 --window-days 3 --rps 3"; log=data/checkpoints/polymarket/log_markets.txt;;
  poly_trades)    pat='download_polymarket.py --phase trades';  cmd=".venv/bin/python scripts/download_polymarket.py --phase trades --rps 4"; log=data/checkpoints/polymarket/log_trades.txt;;
  *) echo "unknown job: $name"; exit 2;;
esac
shift 2 || true
pids() { pgrep -f "$pat" | grep -v "^$$\$" || true; }
case "$action" in
  start)
    if [ -n "$(pids)" ]; then echo "already running: $(pids)"; exit 0; fi
    mkdir -p "$(dirname "$log")"
    nohup $cmd "$@" > "$log" 2>&1 &
    echo "started pid $!";;
  stop)
    p="$(pids)"; if [ -z "$p" ]; then echo "not running"; else kill $p && echo "stopped $p"; fi;;
  status)
    p="$(pids)"; echo "pids: ${p:-none}"; tail -2 "$log" 2>/dev/null;;
esac
