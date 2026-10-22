# Spine

Spine — платформа для корпоративных AI-сценариев, которым нужны проверяемый
контекст, совместная работа агентов и людей и контролируемые действия во внешних
системах.

Проект находится на стадии интеграции и квалификации R1 — Q&A по внутренним
документам. R0-фундамент, versioned ingestion, governed retrieval contracts,
grounded-answer contracts, durable Q&A lifecycle и администраторский web-интерфейс
уже реализованы и проверены на своих границах. Полный production-capable путь
`upload → projection → answer → citations → UI` ещё не квалифицирован: требуется
единая production-композиция `answer_question`, реальный benchmark и финальные
release gates. Legacy Q&A-прототип на Cognee сохраняется как compatibility path.

## Продуктовые кейсы

Spine развивается вертикальными релизами в фиксированном порядке:

1. **Q&A по внутренним документам** — ответы с цитатами и контролем доступа.
2. **Генерация коммерческих предложений** — поиск аналогов, расчёт сметы,
   уточнения и согласование редактируемого черновика.
3. **Мониторинг клиентских коммуникаций** — вопросы без ответа, обязательства,
   претензии и очередь ситуаций, требующих реакции.
4. **Контроль исполнения внутренних задач** — смысловые блокировки,
   ответственность, SLA и эскалации.

Общие компоненты платформы извлекаются из этих кейсов по мере их реализации, а
не разрабатываются заранее как самостоятельный продукт.

## Текущий статус

Канонические определения находятся в
[roadmap](docs/ROADMAP.md#статусы-поставки). В документации используются три
статуса:

- `implemented` — компонент существует и проверен на своей контрактной границе;
- `integrated` — компонент подключён к целевому пользовательскому пути;
- `qualified` — для указанной границы собраны acceptance evidence и пройдены её
  обязательные gates.

Квалификация отдельного компонента не означает, что квалифицирован весь R1 или
реальная внешняя интеграция.

### Реализовано

- PostgreSQL persistence kernel с tenancy/RLS, idempotency и transactional outbox;
- provider-neutral identity и request authorization contracts;
- trace context, structured errors, diagnostic events и append-only audit;
- immutable original-storage contracts и filesystem adapter;
- deployable ingestion composition: PostgreSQL upload ledger, validation,
  parsing, canonical acceptance, initial access policy and transactional outbox;
- bounded outbox dispatcher for durable Ask admission/cancellation and
  provider-backed Cognee projection/recovery requests;
- `SourceObject`/`SourceRevision`, admission decisions, ingestion ledger и
  canonical source/projection lifecycle;
- versioned ingestion PDF, DOCX, XLSX, Markdown и text через `/api/v1`, включая
  stable locators, batch/partial outcomes и Knowledge Control Plane;
- provider-neutral Context Projection и authorized Context Broker с immutable
  Context Bundles, access-policy enforcement и disclosure revalidation;
- grounded `answer_question` contracts: typed outcomes, abstention, claims,
  citations, lineage validation и Q&A run persistence;
- local Agent Runtime: typed schemas and artifacts, explicit registry/bindings,
  diagnostics, cooperative cancellation, replay/recovery и bounded local retry;
- durable Ask Admission через PostgreSQL, transactional outbox и Temporal,
  включая idempotency, retry, cancellation и terminal arbitration;
- React Control Plane: документы и ревизии, ingestion/projection state,
  диагностический Chat, citation inspector, feedback и Q&A runs;
- synthetic regression gate и компоненты real-dataset mapping, canonical corpus
  preparation и post-observation evaluation.

### Operational compatibility path

- загрузка локальных `.md`, `.txt`, `.pdf`, `.docx` и `.xlsx` файлов;
- добавление текстового корпуса в Cognee через `remember`;
- вопросы к корпусу через Cognee `recall`;
- CLI-команды для загрузки данных и вопросов;
- FastAPI endpoints `/health`, `/ingest` и `/ask`;
- diagnostics и authorization composition для API/workers;
- synthetic `answer_question`-shaped handler через общий Agent Runtime contract.

Первые пять пунктов образуют legacy prototype path. Он operational, но не имеет
статуса `integrated`, потому что не входит в целевой governed R1 flow. Команды
быстрого старта ниже относятся именно к этому compatibility path.

### Интегрировано и квалифицировано на ограниченных границах

- supported local ingestion profile квалифицирован с реальными PostgreSQL и
  filesystem adapters;
- PostgreSQL persistence/tenancy, RLS, outbox и durable Ask lifecycle проверены
  на disposable PostgreSQL 17 и Temporal test server;
- Context Projection и authorized Context Broker квалифицированы offline на
  deterministic provider-shaped fakes; production Cognee/Neo4j/PGVector profile
  не квалифицирован;
- `answer_question`, grounded-answer safety и disclosure gates квалифицированы
  offline и на disposable PostgreSQL, без реального LLM/provider execution;
- Knowledge Control Plane и Chat/Runs проходят component, in-process ASGI и
  controlled Playwright gates с synthetic API responses;
- synthetic R1 regression gate воспроизводим и квалифицирован offline;
- operational telemetry, metrics, correlation и no-log UI diagnosis собраны и
  квалифицированы детерминированно, но deployment backup/restore и physical
  provider evidence остаются незавершёнными.

### Что ещё блокирует R1

- единая production-композиция `AnswerQuestionCapability`, общая для API,
  Temporal worker и evaluation observer;
- bounded canonical terminal readback и безопасный typed capture из того же
  authorized execution без повторного retrieval;
- фактический прогон утверждённого private Golden Dataset
  `0.3.9-user-approved` через production application flow;
- утверждение quality/latency/cost/usefulness thresholds и полная end-to-end
  квалификация R0 + R1 через UI;
- deployment-approved backup/restore exercises и production qualification
  выбранного Cognee/Neo4j/PGVector и LLM profile.

Поэтому legacy `/ask` и `/ingest` остаются prototype endpoints, а наличие
квалифицированных компонентов и fake-backed UI journeys не означает готовность
пользовательского R1 или production approval. Канонический статус release gates
зафиксирован в [`docs/qualification/`](docs/qualification/).

## Быстрый старт

Требования:

- Python 3.10–3.14;
- [uv](https://docs.astral.sh/uv/);
- ключ доступа к поддерживаемому Cognee LLM-провайдеру.

Установите зависимости и создайте локальную конфигурацию:

```bash
uv sync
cp .env.example .env
```

Укажите ключ в `.env`:

```dotenv
LLM_API_KEY=your-key
```

При использовании OpenAI-совместимого прокси раскомментируйте соответствующие
`LLM_PROVIDER`, `LLM_MODEL` и `LLM_ENDPOINT` в `.env`.

### Загрузить демонстрационные документы

```bash
uv run spine-seed
```

Команда загружает файлы из `data/fixtures/` в dataset `spine`.

Для собственного корпуса поместите документы в `data/incoming/` или передайте
явный путь:

```bash
uv run spine-seed --path data/incoming
uv run spine-seed --path "/absolute/path/to/documents"
```

### Задать вопрос

```bash
uv run spine-ask "Какие задачи ожидают ответа?"
```

При необходимости можно указать другой dataset:

```bash
uv run spine-ask --dataset my-dataset "О чём говорится в документах?"
```

### Запустить API

```bash
uv run uvicorn spine.api.main:app --reload --port 8080
```

Проверка состояния:

```bash
curl http://localhost:8080/health
```

### Запустить локальный Compose-стенд

Заполните локальный `.env` по `.env.example`, включая отдельные пароли
PostgreSQL, пароль Neo4j, provider bindings и development-only local auth.
Затем соберите и запустите pinned PostgreSQL/PGVector, Temporal, Neo4j/APOC,
API и Web:

```bash
docker compose up --build --detach --wait
curl http://127.0.0.1:4175/api/v1/health/live
docker compose ps --all
```

Web доступен на `http://127.0.0.1:4175`, Temporal UI — на
`http://127.0.0.1:8233`. По умолчанию наружу публикуются только loopback-порты;
их можно переопределить переменными `SPINE_*_PORT`.

Остановить стенд, сохранив данные:

```bash
docker compose down
```

`docker compose down --volumes` удаляет только disposable volumes этого
стенда и используется для полностью чистого повторного bootstrap.

### Run the offline regression gate

The gate replays the synthetic R1 dataset twice without external services,
credentials or production providers. It validates versioned schemas, retrieval,
evidence safety and expected outcomes, and writes a CI artifact:

```bash
uv run spine-eval-gate --output offline-regression-gate.json
```

This is an offline contract gate, not qualification of the real Context Broker,
LLM or production retrieval path.

Вопрос через API:

```bash
curl -X POST http://localhost:8080/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Какие задачи ожидают ответа?"}'
```

Загрузка локальной директории через API:

```bash
curl -X POST http://localhost:8080/ingest \
  -H 'Content-Type: application/json' \
  -d '{"path":"data/incoming"}'
```

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
