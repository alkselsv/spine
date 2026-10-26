# Spine

Spine — платформа для корпоративных AI-сценариев, которым нужны проверяемый
контекст, совместная работа агентов и людей и контролируемые действия во внешних
системах.

## Запуск проекта

Основной локальный способ запуска — полный Compose-стенд. Он поднимает
PostgreSQL/PGVector, Temporal, Temporal UI, Neo4j/APOC, API, фоновые обработчики
и web-интерфейс Control Plane. Команды запуска обёрнуты в `Makefile`, чтобы один
и тот же Compose project name и оба env-файла использовались согласованно.

### 1. Установите необходимые инструменты

Потребуются:

- Docker с Compose v2;
- GNU Make;
- Python 3.10–3.14;
- [uv](https://docs.astral.sh/uv/);
- ключи выбранных LLM- и embedding-провайдеров, если стенд должен выполнять
  реальные provider-вызовы.

Проверьте окружение:

```bash
docker --version
docker compose version
make --version
uv --version
```

Установите Python-зависимости, включая инструменты разработки:

```bash
make install
```

Node.js и pnpm для обычного Compose-запуска на хосте не требуются: web-интерфейс
собирается внутри Docker image.

### 2. Подготовьте runtime-конфигурацию

При первом запуске создайте локальный `.env` из шаблона и ограничьте доступ к
нему. Если рабочий `.env` уже существует, не копируйте шаблон поверх него.

```bash
cp .env.example .env
chmod 600 .env
```

Заполните все значения `<...>` в `.env`. Файл разделён на тематические секции;
для запуска особенно важны:

- `LLM_*` и `EMBEDDING_*` — провайдеры, модели, endpoints и API keys;
- `SPINE_DATABASE_*` и `SPINE_NEO4J_PASSWORD` — независимые локальные пароли;
- `SPINE_LOCAL_AUTH_*` и `SPINE_API_CURSOR_SECRET` — development-only
  аутентификация и секреты длиной не менее 32 символов;
- `SPINE_QA_DEPLOYMENT_COMMIT_SHA` — текущий полный Git SHA;
- `SPINE_QA_DEPLOYMENT_SETTINGS_JSON` — согласованный immutable deployment
  profile; его `commit_sha`, release/model identifiers должны совпадать с
  соседними переменными, а fingerprint должен быть 64-символьным hex-значением
  и меняться при изменении связанной конфигурации;
- `SPINE_COGNEE_WORKER_BINDING_JSON` — одобренная привязка профиля Cognee с
  корректным digest. Схема описана в
  [документе о worker binding](docs/qualification/issue-533-cognee-worker-binding.md).

Для локальных случайных секретов можно использовать `openssl rand -hex 32`, а
текущий commit получить командой `git rev-parse HEAD`. Не используйте одинаковый
секрет для разных ролей, кроме явно описанного в `.env.example` локального
совпадения operator-пароля и пароля БД Cognee.

Убедитесь, что в файле не осталось шаблонных значений:

```bash
grep -nE '<[^>]+>' .env
```

Команда не должна ничего вывести. Не коммитьте `.env`: в нём находятся секреты
локального стенда.

### 3. Создайте временные настройки квалификации

Runtime-настройки и временные полномочия намеренно разделены. После заполнения
`.env` сгенерируйте `.env.qualification`:

```bash
make qualification-env
```

По умолчанию файл:

- действует четыре часа;
- создаётся с правами `0600`;
- не разрешает внешние provider-вызовы и расходы;
- привязывается к deployment и областям доступа из `.env`.

Изменить срок действия можно в пределах от пяти минут до семи дней:

```bash
make qualification-env QUALIFICATION_TTL=30m
make qualification-env QUALIFICATION_TTL=2d
```

Генератор не перезаписывает существующий файл. Для ротации истёкших полномочий
нужно явно разрешить замену:

```bash
make qualification-env FORCE=1
```

Если нужны реальные provider-вызовы, задайте заранее согласованный бюджет:

```bash
make qualification-env PROVIDER_BUDGET=1.00 PROVIDER_CURRENCY=USD
```

Бюджет не может превышать предел из `SPINE_COGNEE_WORKER_BINDING_JSON`, а его
валюта должна совпадать с валютой профиля. Ручной вариант конфигурации приведён
в `.env.qualification.example`.

### 4. Запустите Compose-стенд

```bash
make start
```

Первый запуск занимает больше времени, поскольку Docker скачивает базовые
образы и собирает приложения. `make start` последовательно:

1. проверяет наличие `.env` и `.env.qualification`;
2. запускает PostgreSQL и выполняет bootstrap ролей и Alembic migrations;
3. записывает временные полномочия локальной квалификации;
4. собирает и запускает весь стенд;
5. ждёт успешных health checks.

`make start` не генерирует и не перезаписывает env-файлы. Это защищает от
неявной ротации identity, полномочий и разрешения на расходы.

Обычный `web` собирается без browser fixtures: параметр `?fixture=...` на порту
`4175` не включает синтетические данные. Для отдельного fixture-интерфейса
используйте профиль `fixtures`:

```bash
make start-fixtures
```

Он запускает отдельную сборку `web-fixtures` на
<http://127.0.0.1:4176>. Обычный и fixture-интерфейсы можно держать запущенными
одновременно; порт fixture-сервиса задаётся через `SPINE_WEB_FIXTURES_PORT`.
Страницы фикстур открываются по обычным маршрутам без query-параметров:
`/chat`, `/knowledge/documents` и `/qa/runs`.

### 5. Проверьте запуск

```bash
make status
curl -fsS http://127.0.0.1:18080/api/v1/health/live
curl -fsS http://127.0.0.1:4175/api/v1/health/live
```

После запуска доступны:

- Control Plane: <http://127.0.0.1:4175>;
- Fixture Control Plane: <http://127.0.0.1:4176> после `make start-fixtures`;
- API напрямую: <http://127.0.0.1:18080>;
- Temporal UI: <http://127.0.0.1:8233>;
- Neo4j Browser: <http://127.0.0.1:17474>.

Все порты публикуются только на `127.0.0.1`. При конфликте измените
соответствующие `SPINE_*_PORT` в `.env` и повторите запуск.

Сервисы `database-bootstrap`, `qualification-authority-provisioner`,
`qualification-authority-verifier` и `original-storage-bootstrap` выполняют
одноразовую подготовку и могут отображаться как завершённые с кодом `0`. API,
workers, PostgreSQL, Temporal, Neo4j и web должны оставаться запущенными.

### Повторный запуск и диагностика

Для повторного запуска с сохранёнными данными снова используйте:

```bash
make start
```

Посмотреть последние 200 строк логов основных приложений:

```bash
make logs
```

Изменить глубину и набор сервисов:

```bash
make logs LOG_TAIL=500 LOG_SERVICES="api qa-worker outbox-dispatcher"
```

Если временные полномочия истекли, сначала обновите их, затем снова запустите
стенд:

```bash
make qualification-env FORCE=1
make start
```

Если `database-bootstrap` завершается с ошибкой после изменения несовместимой
локальной схемы, удалите disposable volumes и выполните чистый bootstrap. Это
удалит все локальные данные стенда:

```bash
make destroy
make start
```

### Остановка и удаление локальных данных

Остановить контейнеры, сохранив PostgreSQL, Neo4j и оригиналы документов в
volumes:

```bash
make stop
```

Удалить контейнеры, сеть и все volumes проекта без возможности восстановления:

```bash
make destroy
```

По умолчанию Make использует Compose project name `spine`. Для нескольких
изолированных стендов передавайте одно и то же имя во все команды:

```bash
make start COMPOSE_PROJECT=spine-local-2
make status COMPOSE_PROJECT=spine-local-2
make logs COMPOSE_PROJECT=spine-local-2
make stop COMPOSE_PROJECT=spine-local-2
make destroy COMPOSE_PROJECT=spine-local-2
```

`make destroy` затрагивает только выбранный Compose-проект, но необратимо удаляет
его PostgreSQL, Neo4j и хранилище оригиналов.

### Дополнительный локальный CLI

Для изолированной работы с исходным Q&A CLI без полного Control Plane можно
использовать команды ниже. Они читают runtime-настройки из `.env`.

Загрузить демонстрационный корпус из `data/fixtures/`:

```bash
uv run spine-seed
```

Загрузить собственные документы:

```bash
uv run spine-seed --path data/incoming
uv run spine-seed --path "/absolute/path/to/documents"
```

Задать вопрос:

```bash
uv run spine-ask "Какие задачи ожидают ответа?"
uv run spine-ask --dataset my-dataset "О чём говорится в документах?"
```

Запустить совместимый API отдельно от Compose:

```bash
make api
curl -fsS http://127.0.0.1:8080/health
```

### Offline regression gate

Offline gate не требует внешних сервисов или provider credentials. Он дважды
воспроизводит синтетический R1 dataset и проверяет схемы, retrieval, evidence
safety и ожидаемые результаты:

```bash
uv run spine-eval-gate --output offline-regression-gate.json
```

Этот прогон проверяет offline-контракты, но не реальный Context Broker, LLM или
production retrieval path.

## Локальные данные

- `data/fixtures/` содержит синтетический демонстрационный корпус.
- `data/incoming/` предназначен для локальных рабочих документов и не
  коммитится.
- `.spine/` содержит локальные данные, cache и журналы Cognee и не коммитится.
- Не добавляйте в Git реальные клиентские документы, переписку, credentials или
  неанонимизированные eval-примеры.

## Архитектура

Подробные документы: [целевая архитектура](docs/ARCHITECTURE.md),
[процесс разработки агентов](docs/AGENT_DEVELOPMENT.md),
[процесс разработки workflow](docs/WORKFLOW_DEVELOPMENT.md),
[требования к интерфейсу](docs/INTERFACE.md), [словарь терминов](docs/GLOSSARY.md)
и [порядок релизов](docs/ROADMAP.md).

Целевая система строится как модульный монолит с направленными внутрь
зависимостями:

```text
web / api / workers
        ↓
application / workflows / agents / evaluations / ingestion
        ↓
domain
```

Основные правила:

- PostgreSQL — целевой источник истины для состояния Spine.
- Cognee — перестраиваемая графовая и векторная проекция за контрактом Context
  Broker.
- Процесс зависит от capability и типизированного артефакта, а не от конкретного
  агентского фреймворка.
- Любой значимый вывод сохраняет происхождение и ссылки на доказательства.
- Интерфейс входит в каждый пользовательский вертикальный срез.

Ключевые директории:

```text
src/spine/
  domain/          чистые бизнес-модели и инварианты
  application/     команды, запросы и policies
  auth/            identity и request-authorization contracts
  api/             FastAPI control plane, /api/v1 resources и legacy endpoints
  connectors/      интеграции с внешними источниками
  ingestion/       целевой ingestion и identity resolution
  memory/          Context Broker и Cognee adapter
  agents/          framework-independent agent runtime
  workflows/       durable workflow adapters
  evaluations/     evaluators; eval datasets/runners находятся в evals/
  infrastructure/  persistence, outbox, storage и telemetry
migrations/         линейные Alembic revisions канонической PostgreSQL schema
web/               React Control Plane для R1 Knowledge, Chat и Runs
tests/             автоматические проверки
```

## Разработка

Установите development-зависимости и запустите проверки:

```bash
make verify
```

Полный набор тестов включает обязательный real-PostgreSQL gate. Поддерживаемая
версия, operator bootstrap, отдельные runtime/migration/test настройки,
Alembic-команды и безопасный explicit-URL режим описаны в
[PostgreSQL bootstrap, migrations, runtime and test harness](docs/postgresql.md).
Опциональная внешняя disposable test database настраивается отдельно по
`.env.test.example`; обычный gate не читает runtime или qualification secrets и
по умолчанию использует Testcontainers.
Для короткого локального цикла `make test-fast` явно пропускает PostgreSQL
acceptance и поэтому не является release gate.

Перед архитектурными или межмодульными изменениями прочитайте
[AGENTS.md](AGENTS.md).

## Документация

- [Архитектура](docs/ARCHITECTURE.md) — границы системы и целевая модель.
- [Roadmap](docs/ROADMAP.md) — порядок релизов, scope и exit criteria.
- [Продуктовые кейсы](docs/cases/) — исходные требования.
- [Auth contracts](docs/auth-contracts.md) — identity, route policy и
  authorization-directory seam.
- [Настройки engineering skills](docs/agents/) — issue tracker, triage и domain
  documentation workflow.
- [Skills Matt Pocock](docs/agents/matt-pocock-skills.md) — выбор skill и примеры
  использования в Spine.
