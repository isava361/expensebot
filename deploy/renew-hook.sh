#!/bin/bash
# Install as /etc/letsencrypt/renewal-hooks/deploy/expensebot-nginx, root:root, 0755.
# This hook acts only on the ExpenseBot certificate.
set -euo pipefail
if [ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/expense.ivansavelyev.ru ]; then
    /usr/local/libexec/expensebot-reload-nginx https
fi
