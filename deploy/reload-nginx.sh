#!/bin/bash
# Install as /usr/local/libexec/expensebot-reload-nginx, root:root, 0755.
# Usage: expensebot-reload-nginx http|https. Never restarts Nginx.
set -euo pipefail
mode=${1:-https}
case "$mode" in http|https) ;; *) exit 2 ;; esac
domain=expense.ivansavelyev.ru
lineage=/etc/letsencrypt/live/expense.ivansavelyev.ru
since=$(date --iso-8601=seconds)
master=$(systemctl show nginx -p MainPID --value)
test "$master" -gt 1
old_workers=$(pgrep -P "$master" | sort -n || true)
nginx -t
systemctl reload nginx
applied=false
for attempt in {1..15}; do
    new_workers=$(pgrep -P "$master" | sort -n || true)
    if [ -n "$new_workers" ] && [ "$new_workers" != "$old_workers" ]; then
        if [ "$mode" = http ]; then
            if curl --noproxy '*' --max-time 3 -sS -D - -o /dev/null \
                --resolve "$domain:80:127.0.0.1" "http://$domain/" \
                | tr -d '\r' | grep -qi '^X-Expense-Config: expensebot-v1$'; then
                applied=true
                break
            fi
        else
            if curl --noproxy '*' --max-time 3 -fsS -D - \
                --resolve "$domain:8443:127.0.0.1" "https://$domain:8443/healthz" \
                | tr -d '\r' | grep -qi '^X-Expense-Config: expensebot-v1$'; then
                disk=$(openssl x509 -in "$lineage/fullchain.pem" -noout -fingerprint -sha256)
                served=$(timeout 5 openssl s_client -connect 127.0.0.1:8443 -servername "$domain" </dev/null 2>/dev/null \
                    | openssl x509 -noout -fingerprint -sha256) || served=''
                if [ "$disk" = "$served" ]; then
                    applied=true
                    break
                fi
            fi
        fi
    fi
    sleep 1
done
journalctl -u nginx --since "$since" --no-pager
tail -n 40 /var/log/nginx/error.log
if [ -f /var/log/nginx/expensebot.error.log ]; then tail -n 20 /var/log/nginx/expensebot.error.log; fi
if [ "$applied" != true ]; then
    echo 'ERROR: Nginx reload not confirmed by new workers and real responses.' >&2
    exit 1
fi
echo "ExpenseBot Nginx reload verified ($mode)."
