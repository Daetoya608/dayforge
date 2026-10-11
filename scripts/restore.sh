#!/usr/bin/env bash
# Восстановление Dayforge на новом сервере из снапшота restic (docs/operations.md#восстановление).
# Шаги 3–4: БД и .env системы на место, клонирование проектов с GitHub, .env проектов.
#
# Использование (от root, после установки по docs/operations.md, сервис ещё не запущен):
#   restic restore latest --target /tmp/restore
#   scripts/restore.sh /tmp/restore
set -euo pipefail

SNAPSHOT="${1:?путь к восстановленному снапшоту restic}"
BASE_DIR="${BASE_DIR:-/srv/dayforge}"
SYSTEM_USER="${SYSTEM_USER:-dayforge}"
AGENT_USER="${AGENT_USER:-dayforge-agent}"
STAGE="$(find "$SNAPSHOT" -type d -name backup -path '*/data/backup' | head -n1)"
[ -n "$STAGE" ] || { echo "в $SNAPSHOT нет data/backup" >&2; exit 1; }

echo "БД → $BASE_DIR/data/dayforge.sqlite3"
install -o "$SYSTEM_USER" -g "$SYSTEM_USER" -m 0600 "$STAGE/dayforge.sqlite3" "$BASE_DIR/data/dayforge.sqlite3"

SYSTEM_ENV="$(find "$SNAPSHOT" -type f -name .env -path "*${BASE_DIR}/.env" | head -n1)"
if [ -n "$SYSTEM_ENV" ]; then
    echo ".env системы → $BASE_DIR/.env"
    install -o "$SYSTEM_USER" -g "$SYSTEM_USER" -m 0600 "$SYSTEM_ENV" "$BASE_DIR/.env"
fi

# Токен GitHub — из .env системы, только в окружение команд git.
set -a
# shellcheck disable=SC1091
. "$BASE_DIR/.env"
set +a
AUTH="$(printf 'x-access-token:%s' "${GITHUB_TOKEN:?нужен GITHUB_TOKEN в .env}" | base64 -w0)"

python3 - "$BASE_DIR/data/dayforge.sqlite3" <<'PY' | while IFS=$'\t' read -r slug url; do
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
for slug, url in db.execute(
    "select slug, remote_url from projects where status != 'archived' and remote_url is not null"
):
    print(f"{slug}\t{url}")
PY
    target="$BASE_DIR/projects/$slug"
    if [ -d "$target/.git" ]; then
        echo "$slug: уже есть, пропускаю"
        continue
    fi
    echo "$slug: клонирую $url"
    sudo -u "$AGENT_USER" env GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
        GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $AUTH" \
        git clone -q "$url" "$target"
    sudo -u "$AGENT_USER" git -C "$target" checkout -q dev
    sudo -u "$AGENT_USER" git -C "$target" config user.name "Dayforge Agent"
    sudo -u "$AGENT_USER" git -C "$target" config user.email "agent@dayforge.local"
    if [ -f "$STAGE/env/$slug.env" ]; then
        install -o "$SYSTEM_USER" -g dayforge-work -m 0660 "$STAGE/env/$slug.env" "$target/.env"
    fi
done

echo "Готово. Дальше: uv run alembic upgrade head и systemctl start dayforge."
