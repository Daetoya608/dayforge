#!/usr/bin/env bash
# Синхронизация с сервером: подготовка (идемпотентно), push кода, обновление, перезапуск.
# Использование: deploy/deploy.sh root@<адрес> [ветка, по умолчанию dev]
set -euo pipefail

REMOTE=${1:?укажи root@адрес}
BRANCH=${2:-dev}
cd "$(dirname "$0")/.."

ssh "$REMOTE" 'bash -s' < deploy/bootstrap.sh
git push -q "ssh://$REMOTE/srv/dayforge/dayforge.git" "$BRANCH:$BRANCH"
ssh "$REMOTE" "BRANCH=$BRANCH bash -s" < deploy/update.sh
