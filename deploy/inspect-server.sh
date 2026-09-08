#!/bin/bash
# Read-only inventory. Run as root over SSH before planning any server changes.
set -eu
date -Is
uname -a
ss -ltnp
systemctl list-units --type=service --state=running --no-pager
systemctl list-timers --all --no-pager
nginx -T
certbot certificates
getent ahosts expense.ivansavelyev.ru
ip -brief address
if command -v ufw >/dev/null; then ufw status; fi
for path in /opt/expensebot /etc/expensebot /var/lib/expensebot /etc/nginx/sites-available/expensebot /etc/nginx/sites-enabled/expensebot; do
    if [ -e "$path" ]; then stat "$path"; fi
done
curl --max-time 15 -sS -o /dev/null -w 'food HTTPS: %{http_code}\n' https://food.ivansavelyev.ru:8443/
curl --max-time 10 -sS -o /dev/null -w 'food loopback: %{http_code}\n' http://127.0.0.1:18081/
