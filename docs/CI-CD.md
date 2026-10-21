# CI/CD — сборка, тесты и автоматический деплой на сервер

## Общая схема

```
PR / push в develop|main
        │
        ▼
  CI (.github/workflows/ci.yml)
    • pytest (юнит-тесты без сети)
    • python -m compileall всех модулей
    • bash -n деплой-скриптов
    • на PR: сборка Docker-образа (без публикации)
        │ merge в main (или ручной запуск)
        ▼
  CD (.github/workflows/cd.yml)
    1. сборка + публикация образа в GHCR:
       ghcr.io/soulj57rus/parser_server:latest
       ghcr.io/soulj57rus/parser_server:<версия из version.py>
       ghcr.io/soulj57rus/parser_server:sha-<7 символов>
    2. маркер деплоя в environment «production»
       (опционально — ручное подтверждение, см. ниже)
    3. SSH на сервер → deploy/deploy.sh <версия>:
         а) стоп старого контейнера, бэкапы database.db / config.toml / cookies.json
         б) ТЕСТЫ новой версии в изолированном контейнере (прод не трогается):
            pytest + проверка импортов + валидация config.toml
         в) замена прод-контейнера (docker compose up -d)
         г) canary-наблюдение 60 сек: статус running, в логах нет
            фатальных ошибок импорта, RestartCount не растёт
         д) любой сбой → АВТОМАТИЧЕСКИЙ откат на предыдущий рабочий тег
```

Итог: новая версия появляется на сервере только после успешных тестов
и минутного наблюдения; если что-то не так — сервер сам возвращается
на предыдущую версию, а job в Actions становится красным.

## Требования к серверу

1. Docker Engine + compose-plugin (`docker compose version` отвечает).
2. Каталог приложения `/opt/avito_parser` (или переопределите `APP_DIR` в
   `.github/workflows/cd.yml`):

   ```
   /opt/avito_parser/
     docker-compose.yml          # из репозитория
     .env                        # IMAGE_TAG=<текущий тег>; deploy.sh сам его правит
     config.toml                 # конфиг парсера
     cookies.json
     database.db
     result/  storage/  logs/    # каталоги данных
     backups/                    # создаётся автоматически при каждом деплое
     deploy/deploy.sh, rollback.sh   # загружаются CD автоматически
   ```

3. Разовый доступ к GHCR (первый пуш пакета приватный):
   - *вариант А:* создайте PAT (classic) со scope `read:packages` и на сервере
     `docker login ghcr.io -u <логин> -p <PAT>`;
   - *вариант Б:* после первого деплоя сделайте пакет публичным:
     `github.com/users/soulj57rus/packages/container/parser_server/settings`.

## Секреты GitHub (Settings → Secrets and variables → Actions)

| Секрет            | Значение                                                     |
|-------------------|--------------------------------------------------------------|
| `SSH_HOST`        | IP или домен сервера                                          |
| `SSH_USER`        | пользователь SSH (должен иметь docker)                        |
| `SSH_PRIVATE_KEY` | приватный ключ целиком, включая `-----BEGIN ... KEY-----`     |
| `SSH_PORT`        | необязательно; по умолчанию 22                                |

Публичная часть ключа добавляется в `~/.ssh/authorized_keys` сервера.
Проверить права пользователя: `ssh <user>@<host> docker compose ps` из каталога `/opt/avito_parser` должна работать.

## Environment «production»

Ручной гейт (по желанию): **Settings → Environments → New environment → `production`,
Required reviewers → ваш аккаунт**. Тогда каждый деплой будет ждать кнопки
«Approve» перед заходом на сервер — удобно, чтобы критические версии
выкатывать по осознанному решению.

## Первый запуск

- Если на сервере парсер ещё НЕ работал: положите конфиг/данные в
  `/opt/avito_parser` и запустите деплой — `deploy.sh` поднимет контейнер
  впервые (`.env` заполнится автоматически; до первого merge в `main`
  можно вручную: `bash deploy/deploy.sh latest`).
- Если парсер уже работает со старым образом `ghcr.io/duff89/parser_avito`:
  перепишите `image:` в серверном `docker-compose.yml` на
  `${IMAGE_BASE:-ghcr.io/soulj57rus/parser_server}:${IMAGE_TAG:-latest}`
  (так уже сделано в репозитории) и задайте `IMAGE_TAG` в `.env`.

## Как читать результат деплоя

- Вкладка **Actions → CD → «Деплой на сервер»**: зелёный job — новая версия
  стоит на сервере и прошла canary; красный — в логе будет строка
  `[deploy][FAIL]` с причиной (тесты, статус контейнера, фатальные импорты),
  а сервер уже сам откатился на предыдущую версию.
- Бэкапы данных — `/opt/avito_parser/backups/*.<timestamp>`.
- `deploy/rollback.sh` — ручной откат:

  ```bash
  ssh <сервер>
  cd /opt/avito_parser
  bash deploy/rollback.sh          # на предыдущий рабочий тег
  bash deploy/rollback.sh 3.2.23   # на конкретный тег
  ```

## Переменные деплой-скриптов (можно переопределить)

| Переменная         | По умолчанию                            | Смысл                                        |
|--------------------|------------------------------------------|----------------------------------------------|
| `APP_DIR`          | `/opt/avito_parser`                     | каталог приложения на сервере                |
| `IMAGE_BASE`       | `ghcr.io/soulj57rus/parser_server`      | реестр/имя образа                            |
| `CONTAINER`        | `avito`                                 | имя контейнера (container_name из compose)    |
| `PRECHECK_SECONDS` | `60`                                    | длительность canary-наблюдения, сек          |
| `TEST_CMD`         | pytest + импорты + проверка config.toml | команда предпусковой проверки нового контейнера |

## Что именно тестируется на сервере перед заменой прод-версии

`deploy.sh` делает `bash -lc "$TEST_CMD"` внутри НОВОГО образа с рабочими
bind-mounts:

1. `pip install -q pytest && python -m pytest tests -q` — весь юнит-набор;
2. `python -c 'import parser_cls, load_config, db_service, models, ...'` —
   реальные импорты всех ключевых модулей (фатальные ошибки сборки отсеиваются ещё до замены);
3. `load_config.load_avito_config('/app/config.toml')` — конфиг на сервере
   совместим с новой версией.

После замены 60 секунд меряется:
- `docker inspect -f '{{.State.Status}}'` == `running`;
- `docker logs` не содержит `ModuleNotFoundError / ImportError / SyntaxError / Traceback`;
- `RestartCount` не вырос (нет краш-лупа).

Если что-то из этого нарушено — сервер немедленно возвращается на
предыдущий image, а workflow краснеет.

Смежная документация: `docs/DOCKER.md` (устройство образа и локальный запуск).
