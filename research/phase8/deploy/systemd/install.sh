#!/bin/bash
# Installs the Phase 8 collector schedule. Run as root on the VPS from this directory.
# Never touches algoedge-phase8.service, never deletes or rewrites evidence,
# never starts the collector directly (the timers + ExecCondition decide).
set -euo pipefail
cd "$(dirname "$0")"
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
EVIDENCE=/opt/algoedge/app/research/phase8/evidence/campaign_status

if [[ -n $(./phase8-collector-guard list | tee /dev/stderr) ]]; then
  echo "A collector is already running (above). Stop it first; refusing to install a second one."; exit 1
fi

# Same interpreter and account as the dashboard, unless PYTHON/RUN_USER are given.
dash_exec=$(systemctl show -P ExecStart algoedge-phase8.service)
PYTHON=${PYTHON:-$(sed -n 's/.*path=\([^ ;]*\).*/\1/p' <<<"$dash_exec")}
RUN_USER=${RUN_USER:-$(systemctl show -P User algoedge-phase8.service)}
RUN_USER=${RUN_USER:-root}
RUN_GROUP=$(id -gn "$RUN_USER")
[[ -x $PYTHON ]] || { echo "python not found: '$PYTHON' - rerun with PYTHON=/path/to/python"; exit 1; }
"$PYTHON" -c 'import sys; print("python:", sys.executable, sys.version.split()[0])'
( cd /opt/algoedge/app && PYTHONPATH=src:. "$PYTHON" -m research.phase8.tools.collector --help >/dev/null ) \
  || { echo "collector module not importable with $PYTHON"; exit 1; }
echo "user: $RUN_USER  group: $RUN_GROUP"

# Evidence directory: created only if missing. An existing directory and its
# files are left exactly as they are (no chmod/chown, nothing deleted).
if [[ ! -d $EVIDENCE ]]; then
  install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0750 "$EVIDENCE"
fi
sudo -u "$RUN_USER" test -w "$EVIDENCE" || { echo "$EVIDENCE not writable by $RUN_USER"; exit 1; }

install -D -m 0755 phase8-collector-guard /usr/local/lib/algoedge/phase8-collector-guard
sed -e "s|@PYTHON@|$PYTHON|" -e "s|@USER@|$RUN_USER|" -e "s|@GROUP@|$RUN_GROUP|" \
  algoedge-phase8-collector.service > /etc/systemd/system/algoedge-phase8-collector.service
install -m 0644 algoedge-phase8-collector.timer algoedge-phase8-collector-stop.service \
  algoedge-phase8-collector-stop.timer /etc/systemd/system/
chmod 0644 /etc/systemd/system/algoedge-phase8-collector.service

systemd-analyze verify /etc/systemd/system/algoedge-phase8-collector{,-stop}.{service,timer}
systemctl daemon-reload
# Timers only. Activating the start timer elapses OnBootSec once; the
# collector then starts only if it is Mon-Fri 09:15-15:30 IST right now.
systemctl enable --now algoedge-phase8-collector.timer algoedge-phase8-collector-stop.timer
echo "installed."
