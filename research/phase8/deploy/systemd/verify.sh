#!/bin/bash
# READ-ONLY post-install verification. Changes nothing.
set -u
hr() { printf '\n===== %s =====\n' "$1"; }
hr "timezone";        timedatectl | grep -E 'Local time|Universal time|Time zone|synchronized'
echo "IST now: $(TZ=Asia/Kolkata date '+%a %F %T %Z')"
hr "timers";          systemctl list-timers --all --no-pager 'algoedge-phase8-collector*'
for t in algoedge-phase8-collector.timer algoedge-phase8-collector-stop.timer; do
  systemctl show "$t" -p UnitFileState -p ActiveState -p TimersCalendar -p TimersMonotonic --no-pager; echo
done
hr "collector service"; systemctl status algoedge-phase8-collector.service --no-pager -n 15
hr "collector ExecStart / condition"
systemctl show algoedge-phase8-collector.service -p ExecCondition -p ExecStart -p User -p Restart -p KillSignal --no-pager
hr "window guard now"; /usr/local/lib/algoedge/phase8-collector-guard window
hr "dashboard (unchanged)"
systemctl show algoedge-phase8.service -p ActiveState -p SubState -p ActiveEnterTimestamp --no-pager
curl -sS -o /dev/null -w 'GET http://127.0.0.1:5181/api/auto-trading/status -> HTTP %{http_code}\n' --max-time 5 \
  http://127.0.0.1:5181/api/auto-trading/status
hr "evidence directory"; ls -la /opt/algoedge/app/research/phase8/evidence/campaign_status
hr "collector processes (must be 0 outside the window, exactly 1 inside)"
running=$(/usr/local/lib/algoedge/phase8-collector-guard list); echo "${running:-none}"
echo "count: $(grep -c . <<<"$running")"
systemctl show algoedge-phase8-collector.service -p MainPID --no-pager
