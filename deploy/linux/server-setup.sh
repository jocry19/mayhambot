#!/bin/bash
# Einmalig als root auf einem frischen Ubuntu/Debian-Server, nachdem der Code in /opt/mayhambot liegt:
#   sudo bash /opt/mayhambot/deploy/linux/server-setup.sh
set -euo pipefail
APP=/opt/mayhambot
apt-get update -q
apt-get install -yq docker.io docker-compose-v2 python3 sqlite3 ufw unattended-upgrades
systemctl enable --now docker
ufw allow OpenSSH && ufw --force enable          # Dashboard bleibt intern (127.0.0.1), Zugriff per SSH-Tunnel
[ -f /swapfile ] || { fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile \
  && echo '/swapfile none swap sw 0 0' >> /etc/fstab; }
mkdir -p "$APP/data" "$APP/logs" "$APP/secrets" /var/backups/mayhambot
chown -R 1000:1000 "$APP/data" "$APP/logs"
chmod 700 "$APP/secrets" /var/backups/mayhambot
[ -f "$APP/.env" ] || cp "$APP/.env.example" "$APP/.env"; chmod 600 "$APP/.env"
cp "$APP"/deploy/linux/*.service "$APP"/deploy/linux/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mayhambot.service mayhambot-watchdog.timer mayhambot-backup.timer
echo "Fertig. Status: cd $APP && python3 deploy/ops.py status"
