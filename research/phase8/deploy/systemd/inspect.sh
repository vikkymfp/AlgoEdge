#!/bin/bash
# READ-ONLY pre-install inspection for the Phase 8 collector schedule. Changes nothing.
set -u
hr() { printf '\n===== %s =====\n' "$1"; }
hr "timedatectl";                 timedatectl
hr "systemd version";             systemctl --version | head -1
hr "Phase 8 / AlgoEdge units";    systemctl list-unit-files --no-pager 'algoedge*' ; systemctl list-units --all --no-pager 'algoedge*'
hr "Phase 8 / AlgoEdge timers";   systemctl list-timers --all --no-pager 'algoedge*'
hr "dashboard unit (algoedge-phase8.service)"
systemctl show algoedge-phase8.service -p ActiveState -p SubState -p User -p Group -p WorkingDirectory -p ExecStart --no-pager
hr "dashboard unit file (Environment/EnvironmentFile lines hidden)"
systemctl cat algoedge-phase8.service --no-pager 2>&1 | grep -viE '^\s*(Environment|EnvironmentFile)\s*='
hr "running collector processes"; out=$("$(dirname "$0")/phase8-collector-guard" list); echo "${out:-none}"
hr "port 5181 listener";          ss -ltnp 'sport = :5181'
hr "dashboard GET status (fields only)"
curl -sS --max-time 5 http://127.0.0.1:5181/api/auto-trading/status | python3 -c 'import json,sys; d=json.load(sys.stdin); print("keys:", sorted(d)); print("enabled:", d.get("enabled"), "killSwitch:", d.get("killSwitch"))' || echo "dashboard not answering on 5181"
hr "evidence directory";          ls -la /opt/algoedge/app/research/phase8/evidence/campaign_status 2>&1 | head -20
