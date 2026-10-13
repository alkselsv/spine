# Spine

Spine — платформа для корпоративных AI-сценариев, которым нужны проверяемый
контекст, совместная работа агентов и людей и контролируемые действия во внешних
системах.

Проект находится на ранней стадии. В репозитории работает legacy Q&A-прототип на
Cognee и реализован значительный R0-фундамент: PostgreSQL persistence и tenancy,
identity/authorization, diagnostics/audit/outbox, immutable original storage,
source/ingestion records и framework-independent local Agent Runtime. Эти
компоненты ещё не соединены в готовый R1-путь с governed retrieval, citations и
веб-интерфейсом.

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

### Implemented

- PostgreSQL persistence kernel с tenancy/RLS, idempotency и transactional outbox;
- provider-neutral identity и request authorization contracts;
- trace context, structured errors, diagnostic events и append-only audit;
- immutable original-storage contracts и filesystem adapter;
- `SourceObject`/`SourceRevision`, admission decisions и ingestion ledger;
- local Agent Runtime: typed schemas and artifacts, explicit registry/bindings,
  diagnostics, cooperative cancellation, replay/recovery и bounded local retry.

### Operational compatibility path

- загрузка локальных `.md`, `.txt`, `.pdf`, `.docx` и `.xlsx` файлов;
- добавление текстового корпуса в Cognee через `remember`;
- вопросы к корпусу через Cognee `recall`;
- CLI-команды для загрузки данных и вопросов;
- FastAPI endpoints `/health`, `/ingest` и `/ask`;
- diagnostics и authorization composition для API/workers;
- synthetic `answer_question`-shaped handler через общий Agent Runtime contract.

Первые пять пунктов образуют legacy prototype path. Остальные два доказывают
совместимость инфраструктурных контрактов. Эти пути operational, но не имеют
статуса `integrated`, потому что не входят в целевой governed R1 flow.

### Integrated

Пока ни один компонент не интегрирован в полный целевой путь
`upload → projection → answer → citations → UI`.

### Qualified

- PostgreSQL persistence/tenancy kernel — repository contract и real-PostgreSQL
  gates при наличии обязательного container runtime;
- diagnostics, audit и outbox delivery foundation — offline qualification;
- identity и authorization foundation — offline qualification, с отдельным
  opt-in live OIDC provider gate;
- local Agent Runtime — deterministic fake-backed offline qualification по
  AC1–AC20 из Issue #104.

Local Agent Runtime qualification не включает реальный model provider, Tool
Gateway, PostgreSQL Artifact persistence, Temporal Activity, Cognee projection
или production integration.

### Ещё не интегрировано в R1

- versioned document ingestion через публичный `/api/v1` contract;
- object-level document access policy в retrieval path;
- versioned Context projection и Cognee adapter за Context Broker;
- реальная capability `answer_question`, grounded outcomes, abstention и
  структурированные citations;
- Knowledge Control Plane, сгенерированный API client и web-интерфейс;
- R1 datasets, regression evaluations и release qualification.

Поэтому `/ask` и `/ingest` остаются prototype endpoints, а наличие
квалифицированных R0-компонентов не означает готовность пользовательского R1.

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
  api/             FastAPI control plane и legacy Q&A endpoints
  connectors/      интеграции с внешними источниками
  ingestion/       целевой ingestion и identity resolution
  memory/          Context Broker и Cognee adapter
  agents/          framework-independent agent runtime
  workflows/       durable workflow adapters
  evaluations/     evaluators и release gates
  infrastructure/  persistence, outbox, storage и telemetry
migrations/         линейные Alembic revisions канонической PostgreSQL schema
web/               каркас продуктового интерфейса
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
