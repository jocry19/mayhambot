#!/bin/bash
# Vom Mac aus: aktuellen Code auf den Server bringen und dort sicher neu starten (Option für später).
#   deploy/push-to-server.sh user@server            nur Code (Daten/.env auf dem Server bleiben)
#   deploy/push-to-server.sh user@server --with-data  zusätzlich: frisches Backup vom Mac auf den Server übertragen
#                                                    und dort einspielen (Umzug Mac -> Server, ohne Verlust)
set -euo pipefail
HOST=${1:?user@server angeben}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DEST=/opt/mayhambot
# code only: everything except secrets, data, logs, venv
tar --exclude=.env --exclude='.env.*' --exclude=.venv --exclude=data --exclude=logs --exclude=secrets \
    --exclude=__pycache__ --exclude=.git --exclude='*keypair*.json' --exclude=id.json -czf - . \
  | ssh "$HOST" "sudo mkdir -p $DEST && sudo tar -xzf - -C $DEST"
if [ "${2:-}" = "--with-data" ]; then
  .venv/bin/python deploy/ops.py backup
  B=$(ls -t "${BACKUP_DIR:-$HOME/mayhambot-backups}"/mayhambot-*.tar.gz | head -1)
  scp "$B" "$HOST:/tmp/mayhambot-migrate.tar.gz"
  ssh "$HOST" "cd $DEST && sudo systemctl stop mayhambot 2>/dev/null || true; \
    sudo python3 deploy/ops.py restore /tmp/mayhambot-migrate.tar.gz --no-stop --with-env \
    && sudo chown -R 1000:1000 data && rm /tmp/mayhambot-migrate.tar.gz"
  echo "Daten übertragen. Mac-Bot jetzt stoppen (deploy/mac/uninstall.sh), sonst handeln zwei Bots!"
fi
ssh "$HOST" "cd $DEST && (systemctl is-enabled mayhambot >/dev/null 2>&1 && sudo systemctl restart mayhambot \
  || sudo bash deploy/linux/server-setup.sh)"
