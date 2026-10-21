#!/usr/bin/env bash
# Ручной откат парсера Avito на предыдущий рабочий тег.
#
# Использование (на сервере, из каталога деплоя):
#   ./rollback.sh             # откат на тег из .deploy_state (его пишет успешный deploy.sh)
#   ./rollback.sh 3.2.23      # откат на конкретный тег образа
#
# Шаги: перезапись IMAGE_TAG в .env -> pull -> docker compose up -d сверх тех же
# bind-mounts, поэтому данные (database.db, config.toml, cookies.json, result/)
# остаются без изменений; при необходимости восстановить их из backups/.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/avito_parser}"
IMAGE_BASE="${IMAGE_BASE:-ghcr.io/soulj57rus/parser_server}"

say() { echo -e "\033[1;32m[rollback]\033[0m $*"; }
die() { echo -e "\033[1;31m[rollback][FAIL]\033[0m $*" >&2; }

TAG="${1:-}"
if [ -z "$TAG" ]; then
    if [ -f "$APP_DIR/.deploy_state" ]; then
        TAG="$(cat "$APP_DIR/.deploy_state")"
        say "Тег предыдущей версии из .deploy_state: $TAG"
    else
        die "Не указан тег, а .deploy_state не найден. Вызов: rollback.sh <тег>"
        exit 1
    fi
fi

cd "$APP_DIR"

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || touch "$ENV_FILE"
cp "$ENV_FILE" "$ENV_FILE.bak"
sed -i '/^IMAGE_TAG=/d' "$ENV_FILE"
echo "IMAGE_TAG=$TAG" >> "$ENV_FILE"

say "Откат на ${IMAGE_BASE}:${TAG}"
docker compose pull -q 2>/dev/null || docker compose pull
docker compose up -d
docker compose ps

say "Готово: работает ${IMAGE_BASE}:${TAG}"
docker logs --tail 5 "${CONTAINER:-avito}" 2>&1 || true
