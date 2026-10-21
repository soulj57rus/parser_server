# Зелёно-синий деплой парсера Avito с тестами новой версии и автооткатом.
#
# Использование (на сервере):
#   ./deploy.sh <тег>          например: ./deploy.sh 3.2.24
#
# Схема:
#   1. Остановка контейнера + бэкапы данных (database.db, config.toml, cookies.json)
#   2. Тесты НОВОГО образа в изолированном контейнере (--rm, прод не трогает)
#   3. Замена: docker compose up -d с новым тегом
#   4. Canary-наблюдение PRECHECK_SECONDS секунд: контейнер жив, импорты не падают
#   5. Любой сбой -> автоматический откат на предыдущий рабочий тег
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/avito_parser}"
IMAGE_BASE="${IMAGE_BASE:-ghcr.io/soulj57rus/parser_server}"
CONTAINER="${CONTAINER:-avito}"
PRECHECK_SECONDS="${PRECHECK_SECONDS:-60}"
# Предпусковая проверка нового образа (исполняется внутри контейнера как bash -lc "$TEST_CMD").
# PY_*-строки используют одинарные кавычки внутри python-кода — двойные зарезервированы
# для bash-обёрток и подставляются ниже через \" при сборке TEST_CMD.
PY_IMPORTS='import parser_cls, load_config, db_service, models, parser.http.client, parser.export.excel, filters.ads_filter'
PY_CONFIG="import load_config; load_config.load_avito_config('/app/config.toml'); print('config.toml OK')"
TEST_CMD="${TEST_CMD:-pip install -q pytest && cd /app && python -m pytest tests -q && python -c \"$PY_IMPORTS\" && python -c \"$PY_CONFIG\"}"

say()  { echo -e "\033[1;32m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[deploy]\033[0m $*"; }
die()  { echo -e "\033[1;31m[deploy][FAIL]\033[0m $*" >&2; }

TAG="${1:?использование: deploy.sh <тег>}"

cd "$APP_DIR"
mkdir -p backups deploy

ENV_FILE=".env"
[ -f "$ENV_FILE" ] || touch "$ENV_FILE"

# ---------- текущий рабочий тег (цель будущего отката) ----------
OLD_TAG="$(grep -oP '(?<=^IMAGE_TAG=).*' "$ENV_FILE" || true)"
OLD_TAG="${OLD_TAG:-latest}"

timestamp="$(date +%F-%H%M%S)"
backup() {  # бэкап создаётся только если файл существует
    if [ -f "$APP_DIR/$1" ]; then
        cp -a "$APP_DIR/$1" "backups/$1.$timestamp"
    fi
}

set_env_tag() {  # перезаписывает IMAGE_TAG в .env (снимок предыдущего .env остаётся в .bak)
    cp "$ENV_FILE" "$ENV_FILE.bak"
    sed -i '/^IMAGE_TAG=/d' "$ENV_FILE"
    echo "IMAGE_TAG=$1" >> "$ENV_FILE"
}

rollback_all() {
    warn "Откат на $OLD_TAG..."
    set_env_tag "$OLD_TAG"
    docker compose up -d || true
    docker compose ps || true
    die "Деплой прерван: на сервере снова работает прежняя версия ($OLD_TAG). Подробности выше."
    exit 1
}

# ---------- 1. Остановка + консистентные бэкапы ----------
say "Деплой версии $TAG (предыдущая: $OLD_TAG)"
docker compose stop || true
backup database.db
backup config.toml
backup cookies.json
say "Бэкапы сохранены: backups/*.$timestamp"

# ---------- 2. Тесты НОВОЙ версии до замены ----------
set_env_tag "$TAG"

say "Загрузка образа ${IMAGE_BASE}:${TAG}"
docker compose pull -q 2>/dev/null || docker compose pull

say "Тесты новой версии в изолированном контейнере (--rm; прод не трогается)..."
if ! docker run --rm \
        --entrypoint /bin/bash \
        -v "$APP_DIR/config.toml:/app/config.toml:ro" \
        -v "$APP_DIR/cookies.json:/app/cookies.json:Z" \
        -v "$APP_DIR/result:/app/result:Z" \
        -v "$APP_DIR/storage:/app/storage:Z" \
        -v "$APP_DIR/logs:/app/logs:Z" \
        "${IMAGE_BASE}:${TAG}" \
        -lc "$TEST_CMD"; then
    die "Тесты новой версии провалились"
    rollback_all
fi
say "Тесты новой версии: OK"

# ---------- 3. Замена прод-контейнера ----------
docker compose up -d
say "Контейнер запущен с образом ${IMAGE_BASE}:${TAG}"

# ---------- 4. Canary-наблюдение ----------
sleep 5
restarted_before="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)"
deadline=$((SECONDS + PRECHECK_SECONDS))
healthy=true
while ((SECONDS < deadline)); do
    status="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    if [ "$status" != "running" ]; then
        die "Контейнер '$CONTAINER' не running (получено: $status)"
        healthy=false
        break
    fi
    if docker logs --since 90s "$CONTAINER" 2>&1 |
            grep -qE 'ModuleNotFoundError|No module named|ImportError|SyntaxError|Traceback \(most recent call last\)'; then
        die "Фатальная ошибка в логах новой версии"
        healthy=false
        break
    fi
    sleep 5
done

restarted_now="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)"
if [ "$restarted_now" -gt "$restarted_before" ] 2>/dev/null; then
    die "Контейнер самоперезапускался (RestartCount:$restarted_before->$restarted_now) — краш-луп"
    healthy=false
fi

if [ "$healthy" != true ]; then
    warn "Последние логи новой версии:"
    docker logs --tail 30 "$CONTAINER" 2>&1 || true
    rollback_all
fi

# ---------- 5. Фиксация ----------
echo "$OLD_TAG" > .deploy_state    # предыдущий тег — для ручного отката
rm -f "$ENV_FILE.bak"
say ""
say "Готово: работает ${IMAGE_BASE}:${TAG}"
say "Предыдущая версия: $OLD_TAG (откат вручную: ./deploy/rollback.sh)"
docker compose ps
docker logs --tail 5 "$CONTAINER" 2>&1 || true
