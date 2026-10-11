#!/usr/bin/env bash
# Подготовка сервера под Dayforge (docs/operations.md). Идемпотентно: можно запускать повторно.
# Запускается от root на сервере; обычно — через deploy/deploy.sh.
set -euo pipefail

BASE=/srv/dayforge
AGENT_HOME=/home/dayforge-agent

getent group dayforge-work >/dev/null || groupadd dayforge-work
id dayforge >/dev/null 2>&1 || useradd -r -m -d "$BASE" -s /bin/bash -G dayforge-work dayforge
id dayforge-agent >/dev/null 2>&1 || useradd -r -m -d "$AGENT_HOME" -s /bin/bash -G dayforge-work dayforge-agent

chmod 0711 "$BASE"
install -d -o dayforge -g dayforge -m 0700 "$BASE/data"
install -d -o dayforge -g dayforge -m 0700 "$BASE/data/logs" "$BASE/data/logs/sessions" "$BASE/data/backup"
install -d -o dayforge-agent -g dayforge-work -m 2770 "$BASE/projects"
install -d -o dayforge -g dayforge -m 0755 "$BASE/app"
[ -d "$BASE/dayforge.git" ] || sudo -u dayforge git init -q --bare "$BASE/dayforge.git"

# Рабочие копии проектов принадлежат разным пользователям одной группы — git должен им доверять.
for user in dayforge dayforge-agent; do
    sudo -u "$user" -H git config --global --get-all safe.directory 2>/dev/null | grep -qx '\*' \
        || sudo -u "$user" -H git config --global --add safe.directory '*'
done
git config --global --get-all safe.directory 2>/dev/null | grep -qx "$BASE/dayforge.git" \
    || git config --global --add safe.directory "$BASE/dayforge.git"

# uv — для пользователя системы.
[ -x "$BASE/.local/bin/uv" ] \
    || sudo -u dayforge -H sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh -s -- -q'

# Claude Code — для пользователя агентов (Codex ставится системно в /usr/local/bin).
[ -x "$AGENT_HOME/.local/bin/claude" ] \
    || sudo -u dayforge-agent -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null

# Вход в CLI: если агент ещё не залогинен, а root залогинен — копия учётных данных.
copy_credentials() {
    local source=$1 target=$2
    if [ -f "$source" ] && [ ! -f "$target" ]; then
        install -d -o dayforge-agent -g dayforge-agent -m 0700 "$(dirname "$target")"
        install -o dayforge-agent -g dayforge-agent -m 0600 "$source" "$target"
        echo "скопировано: $target"
    fi
}
copy_credentials /root/.claude/.credentials.json "$AGENT_HOME/.claude/.credentials.json"
copy_credentials /root/.codex/auth.json "$AGENT_HOME/.codex/auth.json"

# Своп 2 ГБ, если его нет: агенты и тесты проектов на маленьких VPS упираются в память.
if ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap -q /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "своп 2G включён"
fi
echo "bootstrap: готово"
