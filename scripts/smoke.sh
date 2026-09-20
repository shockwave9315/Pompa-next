#!/usr/bin/env bash
# Pompa Next runtime smoke and freshness-measurement report.
#
#   scripts/smoke.sh                 # report against http://127.0.0.1:8001
#   scripts/smoke.sh http://host:8001
#   scripts/smoke.sh --json > status-$(date -u +%Y%m%dT%H%M%SZ).json   # archive raw status
#
# Prints facts only. Exits non-zero when /health, /api/v1/status or
# /api/v1/history cannot be fetched. Uses host python3 when present, otherwise
# the python inside the running backend container.
set -euo pipefail

MODE=report
if [[ "${1:-}" == "--json" ]]; then MODE=json; shift; fi
BASE="${1:-${POMPA_URL:-http://127.0.0.1:8001}}"

read -r -d '' PROGRAM <<'PY' || true
import json, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

mode, base = sys.argv[1], sys.argv[2].rstrip("/")

def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

def fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)

code, status = get("/api/v1/status")
if code != 200:
    fail(f"/api/v1/status HTTP {code}")
if mode == "json":
    print(json.dumps(status, indent=2, ensure_ascii=False))
    sys.exit(0)

code, health = get("/health")
print(f"health            HTTP {code} {health}")
if code != 200:
    fail("/health")

m, r, d = status["mqtt"], status["recorder"], status["database"]
print(f"now               {status['now']}")
print(f"mqtt              connected={m['connected']} epoch={m['epoch']} connects={m['connects']} "
      f"disconnects={m['disconnects']} alive={m['alive']} alive_since={m['alive_since']}")
print(f"                  connected_at={m['connected_at']} disconnected_at={m['disconnected_at']}")
print(f"                  last_live_message_at={m['last_live_message_at']} parse_rejects={m['parse_rejects']} "
      f"stale_after_seconds={m['stale_after_seconds']}")
print(f"lwt               {m['lwt']}")
print(f"recorder          process_start={r['process_start']} last_closed_minute={r['last_closed_minute']} "
      f"last_written_minute={r['last_written_minute']}")
print(f"                  rows_closed={r['rows_closed']} rows_written={r['rows_written']} "
      f"protected_rows={r['protected_rows']} waiting_rows={r['waiting_rows']}/{r['waiting_capacity']} "
      f"dropped_rows={r['dropped_rows']} "
      f"schema_ready={r['schema_ready']} db_last_error={r['db_last_error']}")
print(f"database          available={d['available']} oldest={d['oldest_minute']} newest={d['newest_minute']} "
      f"error={d['error']}")

end = int(time.time()) // 60 * 60
start = end - 3600
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
code, hist = get(f"/api/v1/history?from={iso(start)}&to={iso(end)}&bucket=1m"
                 "&series=main_outlet_temp,co_power_consumption")
if code != 200:
    fail(f"/api/v1/history HTTP {code} {hist}")
buckets = hist["buckets"]
recorded = sum(b["recorded_minutes"] for b in buckets)
gaps, run = [], None
for b in buckets:
    if b["recorded_minutes"] == 0:
        run = run or [b["start"], b["end"]]
        run[1] = b["end"]
    elif run:
        gaps.append(run); run = None
if run:
    gaps.append(run)
print(f"history last 60m  recorded_minutes={recorded}/{len(buckets)} missing_ranges={len(gaps)}")
for g in gaps:
    print(f"                  missing [{g[0]}, {g[1]})")

print()
print("per-topic measurement (process lifetime): gaps are sampled only between consecutive non-retained")
print("messages of one topic inside one observation interval (ended by MQTT reconnect or LWT Offline)")
print(f"{'id':7} {'topic':45} {'seen':5} {'live':>7} {'ret':>4} {'sent':>5} {'rej':>4} {'gaps':>6} "
      f"{'mean_gap_s':>10} {'max_gap_s':>10}  latest_live_at")
fmt = lambda v: "" if v is None else f"{v:.1f}"
for s in status["sources"]:
    print(f"{s['id']:7} {s['topic']:45} {str(s['seen_live']):5} {s['live_messages']:>7} "
          f"{s['retained_messages']:>4} {s['sentinel_messages']:>5} {s['rejected_messages']:>4} "
          f"{s['gap_count']:>6} {fmt(s['mean_live_gap_seconds']):>10} {fmt(s['max_live_gap_seconds']):>10}  "
          f"{s['latest_live_at']}")
print(f"uncatalogued topics seen: {len(m['uncatalogued_topics'])}")
extra = [t for t in m["uncatalogued_topics"] if not t.startswith("main/")]
if extra:
    print("  non-main: " + ", ".join(extra))
PY

if command -v python3 >/dev/null 2>&1; then
  exec python3 -c "$PROGRAM" "$MODE" "$BASE"
fi
cd "$(dirname "$0")/.."
exec docker compose exec -T backend python -c "$PROGRAM" "$MODE" "http://127.0.0.1:${API_PORT:-8001}"
