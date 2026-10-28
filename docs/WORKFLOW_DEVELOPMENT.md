# Разработка workflow в Spine

Статус: руководство по целевому code-first процессу разработки. Domain-модели и
Q&A-specific Temporal execution boundary уже существуют, но универсальные
workflow authoring, compilation, publication и deployment lifecycle реализованы
не полностью. Примеры ниже задают требуемый процесс и границы, а не обещают
наличие всех команд, API и интерфейсов.

Документ описывает создание версионируемого бизнес-процесса из agent, code,
human, gate и subworkflow steps. Разработка реализации отдельной capability и её
выпуск как `Agent Version` описаны в
[AGENT_DEVELOPMENT.md](AGENT_DEVELOPMENT.md). Архитектурные основания находятся
в [ARCHITECTURE.md](ARCHITECTURE.md#11-workflow-orchestration), термины — в
[CONTEXT.md](../CONTEXT.md), а порядок продуктовой поставки — в
[ROADMAP.md](ROADMAP.md).

## 1. Основная идея

Workflow — не агент и не длинный prompt. Это версионируемая схема достижения
бизнес-результата, которая координирует типизированную работу агентов, кода,
людей и вложенных процессов. Workflow владеет долговременным состоянием,
переходами, ожиданиями, retries, approvals и terminal outcomes.

Граница выглядит так:

```text
Capability        какой вид работы требуется
Agent Version     какая версия реализации выполняет capability
Agent Binding     какую версию разрешено выбрать в данном scope
Workflow Version  когда вызвать capability и что делать с результатом
```

Целевой процесс разработки:

```text
бизнес-цель и KPI
  → trigger, WorkItem и terminal outcomes
  → versioned artifacts и handoff contracts
  → step graph: agent | code | human | gate | subworkflow
  → policies, retries, idempotency и approvals
  → code-first Workflow Definition
  → deterministic Temporal implementation и Activities
  → tests, evals и vertical scenario
  → immutable Workflow Version
  → shadow/canary/production deployment
  → outcome feedback и следующая версия
```

## 2. Шаг 1. Зафиксировать бизнес-результат и владельца

Начинать следует не с DAG, Temporal или списка агентов, а с результата для
пользователя и бизнеса. Для генерации коммерческого предложения результат можно
сформулировать так:

> Менеджер получает проверяемый черновик КП и воспроизводимую смету, уточняет
> пробелы, редактирует результат и явно подтверждает отправку клиенту.

До проектирования шагов фиксируются:

- владелец процесса и пользователи результата;
- измеримый outcome и связанные KPI;
- стоимость ошибки и допустимая степень автоматизации;
- решения, требующие человека или policy approval;
- запрещённые действия;
- начальные и terminal states;
- условия отмены, остановки и эскалации;
- rollback owner и операционная ответственность.

Успешный вызов модели или завершённый agent step не является бизнес-результатом.
Outcome оценивается на уровне всего workflow.

## 3. Шаг 2. Определить trigger, WorkItem и область исполнения

Trigger создаёт или продолжает конкретную единицу работы. Он может происходить
из UI, API, расписания, канонического domain event или другого workflow. Для
каждого trigger определяются:

- versioned input schema;
- бизнес-ключ, из которого выводится стабильный workflow ID;
- `workspace_id` и environment;
- actor и его authority;
- правила дедупликации и повторной доставки;
- источник, provenance и correlation identifiers;
- поведение при устаревшем или уже обработанном событии.

Одинаковый бизнес-ключ не должен создавать два независимых процесса. Повторный
trigger либо воспроизводит стабильный результат, либо безопасно продолжает уже
созданный run, либо возвращает явный conflict при изменившейся семантике.

## 4. Шаг 3. Спроектировать artifacts и handoffs

Workflow передаёт между шагами типизированные artifacts, а не chat transcript и
не неструктурированный общий словарь. Для КП это могут быть:

```text
ClientRequest
  → RequirementsArtifact
  → AnaloguesArtifact
  → ProposalStructure
  → Estimate
  → ProposalDraft
  → ApprovedProposal
```

Для каждого artifact задаются:

- schema name, version и правила совместимости;
- обязательные поля и доменные инварианты;
- `workspace_id`, environment и upstream lineage;
- evidence и source revisions, необходимые для принятия;
- статус draft/accepted/rejected/superseded;
- поля, доступные для human edit;
- правила redaction и disclosure;
- способ отображения diff между версиями.

`Handoff` определяет, при каких условиях downstream step может принять artifact.
Он включает evaluators, thresholds и явное `on_fail`: retry, fallback, human или
stop. Citation presence сама по себе не доказывает корректность evidence.

## 5. Шаг 4. Разделить процесс на шаги

Каждый шаг должен иметь одну проверяемую ответственность, типизированный вход и
выход и понятного владельца:

| Тип | Когда использовать | Чего в нём не должно быть |
|---|---|---|
| `agent` | Вероятностная интерпретация или генерация через capability | Durable retries, скрытые approvals, прямой выбор implementation |
| `code` | Детерминированное преобразование или бизнес-правило | Модельная догадка вместо точного расчёта |
| `human` | Решение, уточнение или редактирование, требующее judgement | Неявный ввод вне audit history |
| `gate` | Evaluation, policy, approval или ветвление | Генерация нового бизнес-содержимого без отдельного artifact |
| `subworkflow` | Изолированный переиспользуемый процесс | Скрытая shared mutable state родителя |

Начальный workflow генерации КП:

```text
ClientRequest
  → normalize_request (code)
  → extract_requirements (agent)
  → requirements_handoff (gate)
  ├─ missing → clarify_requirements (human) ─┐
  └─ pass ───────────────────────────────────┘
  → find_analogues (agent)
  → compose_structure (agent)
  → calculate_estimate (code)
  → assemble_draft (agent)
  → validate_proposal (gate)
  → edit_and_approve (human)
  → send_proposal (code/activity)
```

Сначала реализуется минимальный вертикальный путь. Новый branch, subworkflow или
универсальная абстракция добавляются только при наличии продуктового сценария и
теста, который требует их существования.

## 6. Шаг 5. Связать agent steps с capabilities

Agent step указывает стабильную capability и версии входной/выходной схемы, но
не импортирует handler, не выбирает модель и не хранит исполняемый Python path.
Конкретную `Agent Version` выбирает scoped `Agent Binding`.

Для каждого agent step фиксируются:

- capability и совместимые schema versions;
- доступный binding scope;
- handoff gate и обязательные evidence;
- deadline и resource budget;
- поведение при abstention, denial и unavailable runtime;
- допустимый fallback;
- eval suite, применяемый к handoff.

Замена prompt, модели или Agent Version не требует новой Workflow Version, пока
контракт capability и семантика процесса не изменились. Изменение graph,
переходов, policy, artifact contract или observable outcome создаёт новую версию
workflow.

## 7. Шаг 6. Определить human steps, gates и внешние действия

Human step является first-class узлом. Он имеет typed input/output, assignee,
deadline, reminders, варианты решения и audit history. Человек может быть
основным исполнителем шага, а не только аварийным fallback.

Для каждого gate задаются:

- проверяемое условие и версия evaluator/policy;
- входные evidence и artifact versions;
- pass/fail/indeterminate outcomes;
- retry, fallback, human или stop transition;
- безопасная диагностика без защищённого содержимого;
- правила повторной проверки после изменения upstream artifact.

Mutating или внешне видимое действие исполняется только после policy check и,
когда требуется, явного approval. Решение человека связывается с точной версией
artifact; изменение artifact делает прежнее approval непригодным.

## 8. Шаг 7. Спроектировать durable execution

Workflow-код содержит только детерминированное управление состоянием. Сеть,
PostgreSQL, Context Broker, LLM, filesystem и внешние API вызываются через
Activities или другие явно определённые adapter boundaries.

До реализации задаются:

- idempotency key каждого side effect;
- короткие технические Activity retries;
- долговременные workflow retries и fallback transitions;
- deadlines, timers, reminders и escalation;
- cancellation и compensation behavior;
- signals, updates и queries;
- crash/restart recovery;
- terminal states и невозможные переходы;
- retention и безопасная видимость состояния.

Activity должна выдерживать at-least-once execution. Повтор после timeout или
частичного сбоя создаёт один логический эффект и возвращает совместимый
результат. Exactly-once delivery не предполагается.

## 9. Шаг 8. Создать code-first Workflow Definition

На текущем этапе definitions создаются и рецензируются как типизированный код в
Git. Control Plane может показывать graph, diff, runs, deployment и rollback, но
не является источником произвольно изменяемого runtime-кода.

Целевая структура:

```text
src/spine/workflows/
  definitions/
    proposal_generation/
      v1.py
  code_steps/
    proposal_pricing.py
  temporal/
    definitions/
    activities/
```

Business definition описывает identity, owner, trigger, typed steps,
dependencies, transitions, policies и ссылки на capability/code-step keys.
Temporal definition адаптирует эту схему к durable execution.

Definition не должна содержать:

- произвольный import path, загруженный из базы;
- прямой вызов Cognee, LLM или внешнего API;
- недетерминированные clock, randomness или environment reads;
- credentials или runtime secrets;
- конкретный agent handler вместо capability;
- неограниченный динамический выбор tools.

Текущие `WorkflowDefinition`, `WorkflowVersion` и `StepDefinition` являются
начальным domain-контрактом. Полная DSL/schema должна развиваться вместе с
compiler, validation и migration policy, а не только с одним workflow.

### 9.1. Общий контракт исполнения workflow

Workflow Definition не объявляет HTTP routes и не вызывает Temporal напрямую.
Она объявляет типизированный протокол исполнения: trigger, принимаемые команды,
создаваемые artifacts, наблюдаемые состояния и terminal outcomes. Один общий
application interface исполняет этот протокол для всех workflow; HTTP и Temporal
являются adapters на разных seams.

```text
product UI или внешний клиент
  → HTTP adapter
  → workflow application interface
  → domain state и ports
  → Temporal adapter / persistence / outbox
```

Минимальный application interface содержит четыре операции:

```text
start(StartWorkflowCommand) → WorkflowCommandReceipt
dispatch(WorkflowCommand) → WorkflowCommandReceipt
get_run(GetWorkflowRunQuery) → WorkflowRunSnapshot
list_events(ListWorkflowEventsQuery) → WorkflowEventPage
```

Это логический интерфейс, а не требование объединить операции в один Python
class. Command handlers могут быть отдельными application use cases, пока их
семантика и результаты остаются общими. Workflow-specific код не добавляет
собственный execution runtime или persistence protocol.

#### StartWorkflowCommand

Команда запуска содержит:

- `command_id` и `idempotency_key`;
- `workspace_id`, environment и authenticated acting subject;
- `workflow_key` и запрошенную либо выбранную `workflow_version`;
- стабильный `work_item_key`, идентифицирующий единицу бизнес-работы;
- `trigger_schema` и валидированный typed trigger payload;
- provenance, correlation и causation identifiers;
- authority/delegation context, если команда выполняется on behalf of другого
  субъекта.

Выбор версии завершается до создания run. Созданный `WorkflowRun` закрепляет
точную immutable `WorkflowVersion` и не начинает использовать более новую
версию после deployment switch.

Application contract не требует отдельного стартового экрана. Product UI может
собрать trigger через форму, импорт, событие или conversational composer. В
последнем случае первое сообщение входит в typed trigger payload и создаёт run,
а последующие сообщения преобразуются в `WorkflowCommand` того же run. UI не
должен создавать chat session как побочный эффект запуска workflow.

`work_item_key` и `idempotency_key` решают разные задачи. Первый не допускает два
независимых процесса для одной бизнес-работы в одинаковом scope. Второй делает
безопасным повтор конкретной команды. Одинаковый idempotency identity и payload
digest возвращают прежний receipt; тот же identity с другим digest возвращает
conflict.

#### WorkflowCommand

Команда продолжения существующего run содержит:

- `command_id`, `idempotency_key` и `run_id`;
- `workspace_id`, environment и authenticated acting subject;
- `expected_run_version` для optimistic concurrency control;
- стабильный `command_type`;
- `command_schema` и typed payload;
- точные `ArtifactRef`, если решение или переход опирается на artifacts;
- correlation, causation и authority/delegation context.

`command_type` не является именем Temporal Signal/Update. Например,
`submit_intake_message`, `revise_proposal` или `decide_artifact` выражают
бизнес-намерение. Temporal adapter самостоятельно выбирает механизм доставки.
Browser, product UI и Workflow Definition не зависят от task queue, workflow ID,
Signal, Update или Query names.

Для каждой пары `(workflow_key, workflow_version, command_type)` registry хранит
конкретную versioned schema. Transport adapter может принять JSON, но обязан
преобразовать его в соответствующую typed model с `extra="forbid"` до вызова
application handler. Произвольный `dict` не является workflow-контрактом.

Команда принимается только если одновременно выполняются следующие условия:

- actor сейчас авторизован в указанном workspace и environment;
- run принадлежит тому же scope без раскрытия существования чужого run;
- command schema поддерживается закреплённой Workflow Version;
- команда разрешена текущим состоянием и policy;
- `expected_run_version` совпадает с текущей версией;
- все ArtifactRef указывают на доступные, подходящие и неустаревшие версии;
- повтор команды согласуется с её idempotency receipt.

Успешный replay повторно проверяет текущую авторизацию. Прежний receipt не даёт
права увидеть защищённый result после отзыва доступа.

#### WorkflowCommandReceipt

Receipt подтверждает admission команды, а не завершение всего workflow. Он
содержит:

- `command_id`, `run_id` и результат admission: `accepted` или `replayed`;
- актуальную либо созданную `run_version`;
- текущий disclosure-safe run status;
- ссылку на snapshot/events для дальнейшего наблюдения;
- safe rejection category при conflict или невозможности принять команду.

HTTP adapter обычно возвращает такой receipt с `202 Accepted`. Синхронное
завершение короткого command handler не превращает долгий workflow в HTTP
request/response loop.

#### WorkflowRunSnapshot

Snapshot — read model для UI и integrations, а не сериализованное внутреннее
состояние Temporal. Он содержит как минимум:

- `run_id`, `work_item_id`, workflow key и закреплённую version;
- workspace/environment scope и monotonic `run_version`;
- lifecycle state и terminal outcome, если он достигнут;
- текущие steps, ожидания, deadlines и disclosure-safe failure summaries;
- доступные текущему actor `allowed_commands` со ссылками на schemas;
- ArtifactRef и требующие решения ApprovalRequest без встраивания защищённого
  содержимого;
- timeline cursor, trace и timestamps.

`allowed_commands` помогает UI построить controls, но не является
авторизацией. Сервер заново проверяет каждую отправленную команду. Snapshot не
показывает hidden chain-of-thought, provider payloads, secrets или raw exception.

WorkflowRun не является chat session. Чат может быть одним из product views над
run и отправлять workflow commands, но запуск workflow создаёт `WorkItem` и
`WorkflowRun`, а не новую `Q&A Session`. Transcript не служит durable workflow
state и не заменяет artifacts.

#### Ошибки и конкуренция

Общий контракт различает как минимум:

- validation error — payload не соответствует зарегистрированной schema;
- denied — actor не имеет текущего права выполнить или увидеть операцию;
- not found — безопасный ответ без утечки существования объекта;
- idempotency conflict — ключ повторён с другой семантикой;
- stale run — `expected_run_version` устарел;
- invalid transition — команда недопустима в текущем состоянии;
- stale artifact — решение относится не к активной версии artifact;
- unavailable/degraded — исполнение временно невозможно;
- terminal run — run уже завершён и не принимает эту команду.

Повтор после timeout обязан быть безопасным. Клиент при `stale run` получает
новый snapshot и предлагает пользователю повторить осмысленное действие, а не
молча перезаписывает параллельное решение.

#### HTTP mapping

Публичный control-plane adapter использует общий namespace и не создаёт новый
набор routes для каждого step:

```text
POST /api/v1/runs
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/commands
GET  /api/v1/runs/{run_id}/events

GET  /api/v1/artifacts/{artifact_id}
GET  /api/v1/artifacts/{artifact_id}/versions

GET  /api/v1/approvals
POST /api/v1/approvals/{approval_id}/decision
```

Последний route является удобным control-plane представлением общей
`decide_artifact`/approval command и вызывает тот же application handler. Узкий
product facade, например `/api/v1/proposals`, допустим только когда выражает
устойчивую бизнес-семантику; он также адаптирует запросы к общему workflow
interface и не создаёт второй источник state.

Actor и scope выводятся из проверенного request context и сверяются с
идентификаторами command; клиент не может назначить себе authority полями JSON.
OpenAPI описывает envelopes и все встроенные command payloads. TypeScript types
генерируются из OpenAPI в `web/api/generated/` и не редактируются вручную.

В первой поставке registry содержит только schemas workflow, выпускаемых вместе
со Spine. Загрузка произвольных внешних JSON Schemas и динамическая генерация
routes не входят в этот контракт: такая generalization требует отдельного
продуктового сценария, threat model и versioning design.

## 10. Шаг 9. Реализовать Temporal adapter и Activities

Temporal отвечает за event history, durable waits, timers, signals, retries и
восстановление процесса. PostgreSQL остаётся каноническим store Spine-owned
definitions, artifacts, approvals, outcomes и audit records; Temporal history —
источник истины исполнения конкретного durable run.

Реализация должна:

- регистрировать только одобренные workflow и Activity definitions;
- закреплять task queue и совместимую worker version;
- передавать typed identifiers вместо больших provider payloads;
- не читать изменяемую конфигурацию внутри replayable workflow code;
- сохранять artifact и audit state через application ports;
- поддерживать cooperative cancellation;
- безопасно переживать worker restart и повтор Activity;
- сохранять trace/correlation через workflow, steps и внешние вызовы.

Долгое ожидание approval не реализуется polling-циклом агента или API-процесса:
workflow ожидает Signal/Update и может одновременно обслуживать Query.

## 11. Шаг 10. Написать тесты

Тесты доказывают публичные outcomes и переходы, а не внутренний порядок вызовов.
Минимальный набор:

- domain tests валидности graph, schemas и terminal transitions;
- application tests через fake capabilities, repositories и policy ports;
- contract tests для Activities, handoffs и artifact persistence;
- Temporal tests с time skipping и fake Activities;
- allowed, denied и cross-workspace isolation scenarios;
- повтор trigger и повтор Activity с тем же idempotency key;
- partial failure, retry exhaustion и fallback;
- cancellation до, во время и после side effect;
- signals, updates, queries, deadlines и human wait;
- stale, inaccessible и fabricated evidence;
- replay compatibility закреплённой Workflow Version;
- запрет внешнего действия без подходящего approval.

Сначала запускается узкий тест изменяемого перехода, затем репозиторные gates:

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

Реальные model providers и внешние системы проверяются отдельными opt-in
integration tests; offline suite использует contract fakes.

## 12. Шаг 11. Проверить вертикальный сценарий

Перед выпуском выполняется полный пользовательский путь:

```text
загрузить тестовые источники
  → принять trigger и создать WorkItem
  → выполнить agent/code steps
  → проверить artifacts, lineage и handoff gates
  → запросить clarification/approval
  → возобновить тот же run
  → получить ProposalDraft и Estimate
  → выполнить разрешённое внешнее действие
  → записать OutcomeRecord
```

Проверяются:

- закреплённые версии workflow, agents, prompts, context и evaluators;
- lineage каждого промежуточного artifact;
- корректность и доступность evidence;
- воспроизводимость денежных расчётов;
- отсутствие side effect до approval;
- повторяемость результата после retry/replay;
- loading, waiting, partial, degraded, denied и terminal UI states;
- единый trace от trigger до outcome и внешнего действия.

## 13. Шаг 12. Выпустить immutable Workflow Version

Workflow Version закрепляет как минимум:

- checksum business definition и implementation release;
- trigger и artifact schema versions;
- step graph, transitions и terminal states;
- capability и code-step keys;
- handoff evaluators, policies и thresholds;
- retry, timeout, cancellation и compensation rules;
- human ownership, deadlines и approval requirements;
- совместимые worker/runtime versions;
- eval dataset, validation report и rollback target;
- owner, risk tier и deployment scope.

Публикация блокируется при несовместимых schemas, cycles, dead ends,
неразрешённых capabilities/tools, отсутствующем idempotency contract, провале
evals или невозможности rollback. Опубликованная версия не редактируется:
изменение выпускается новой версией, а активный run продолжает использовать
закреплённый execution snapshot.

## 14. Шаг 13. Развернуть, наблюдать и откатить

Жизненный цикл:

```text
draft
  → validation and offline eval
  → shadow
  → canary deployment
  → production deployment
  → continuous handoff/outcome evaluation
  → rollback или superseded
```

Shadow не выполняет внешние действия и не влияет на пользователя. Canary
ограничивается явным workspace/environment/trigger scope. Rollback переключает
новые runs на последнюю подходящую версию; для уже активных runs заранее
определяется continue, migrate, compensate или cancel policy.

Наблюдаемость должна отвечать на вопросы:

- какая версия и trigger создали run;
- на каком step находится WorkItem и чего он ожидает;
- какие artifacts и evidence прошли каждый handoff;
- какие retries, fallbacks, approvals и external actions произошли;
- где выросли latency, cost или human correction rate;
- какой outcome и KPI достигнуты;
- можно ли безопасно остановить deployment и оценить blast radius.

## 15. Шаг 14. Вернуть feedback в разработку

Правки человека, gate failures, cancellations, retries и production outcomes не
изменяют definition автоматически. После review они превращаются в новые
fixtures, eval cases или явно спроектированную следующую Workflow Version.

```text
production observation
  → подтверждённая проблема или opportunity
  → новый test/eval case
  → изменение graph/policy/capability contract
  → regression и vertical comparison
  → новая immutable Workflow Version
```

Process mining может предложить `AutomationOpportunity`, но не публикует и не
меняет production workflow без решения владельца процесса.

## 16. Definition of Done для нового workflow

Workflow готов к production deployment, когда:

- бизнес-результат, KPI, owner и запрещённые действия определены;
- trigger имеет versioned schema, tenant scope и idempotent business key;
- входные, промежуточные и выходные artifacts типизированы и версионированы;
- step graph ацикличен, не содержит dead ends и имеет terminal outcomes;
- каждый agent step ссылается на capability, а не конкретную реализацию;
- code, human, gate и subworkflow boundaries выбраны явно;
- handoff policies проверяют schema, evidence, access и quality;
- side effects имеют policy/approval и idempotency contracts;
- retries, fallback, timeout, cancellation и recovery определены;
- Temporal workflow детерминирован, Activities безопасны при повторе;
- workspace isolation и отсутствие утечек доказаны тестами;
- time-skipping, failure, replay и vertical scenarios проходят;
- Workflow Version immutable и имеет validation report и rollback target;
- UI показывает состояние, ожидание, evidence, решение и terminal failure;
- runs сохраняют versions, lineage, audit, metrics, trace и OutcomeRecord.

Если хотя бы один обязательный пункт нельзя подтвердить, workflow остаётся draft
или shadow и не получает production deployment.
