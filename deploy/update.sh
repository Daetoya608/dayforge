#!/usr/bin/env bash
# Обновление Dayforge на сервере из bare-репозитория и перезапуск службы.
# Запускается от root на сервере; обычно — через deploy/deploy.sh. BRANCH — ветка (dev).
set -euo pipefail

BASE=/srv/dayforge
APP=$BASE/app
BRANCH=${BRANCH:-dev}
UV=$BASE/.local/bin/uv

chown -R dayforge:dayforge "$BASE/dayforge.git"
sudo -u dayforge git --git-dir="$BASE/dayforge.git" --work-tree="$APP" checkout -q -f "$BRANCH"
echo "код: $(sudo -u dayforge git --git-dir="$BASE/dayforge.git" log -1 --format='%h %s' "$BRANCH")"

install -m 0755 "$APP/deploy/dayforge-agent-exec" /usr/local/bin/dayforge-agent-exec
install -m 0440 "$APP/deploy/sudoers" /etc/sudoers.d/dayforge
visudo -cqf /etc/sudoers.d/dayforge
install -m 0644 "$APP/deploy/dayforge.service" /etc/systemd/system/dayforge.service
systemctl daemon-reload

cd "$APP"
sudo -u dayforge -H "$UV" sync --frozen --no-dev -q

if [ ! -f "$BASE/.env" ]; then
    echo "нет $BASE/.env — миграции и запуск пропущены"
    exit 0
fi
sudo -u dayforge -H bash -c "set -a; . '$BASE/.env'; set +a; cd '$APP' && .venv/bin/alembic upgrade head" 2>&1 | grep -v '^INFO  \[alembic.runtime.migration\] \(Context\|Will\)' || true
systemctl enable -q dayforge
systemctl restart dayforge
sleep 3
systemctl is-active dayforge
