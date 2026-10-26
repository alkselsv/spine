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
