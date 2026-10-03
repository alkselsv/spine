# Разработка агентов в Spine

Статус: руководство по целевому процессу разработки. Agent runtime, Context
Broker, versioned registry, Temporal workflows и release gates пока реализованы
не полностью; примеры ниже задают направление реализации, а не утверждают, что
все команды и интерфейсы уже доступны.

Этот документ описывает последовательный code-first процесс разработки агента в
Spine. Сквозной пример — подготовка коммерческого предложения по первичному
запросу клиента и корпоративной базе знаний.

Архитектурные основания процесса определены в
[ARCHITECTURE.md](ARCHITECTURE.md), термины — в [CONTEXT.md](../CONTEXT.md), а
граница локального runtime — в
[ADR 0010](adr/0010-capability-based-agent-runtime.md). Требования к самому кейсу
зафиксированы в [ROADMAP.md](ROADMAP.md#r2--генерация-коммерческих-предложений) и
[описании кейса](cases/ИИ-ассистент%20для%20генерации%20коммерческих%20предложений.md).

## 1. Основная идея

Агент разрабатывается не как автономный чат-бот, которому передают все документы
и общую инструкцию. Разработчик создаёт проверяемую реализацию конкретной
`Capability` с типизированными входом и выходом. Агент получает только разрешённый
контекст, возвращает структурированный результат с evidence и выполняется как
один из шагов наблюдаемого workflow.

Для генерации КП итоговый процесс выглядит так:

```text
бизнес-требование
  → типизированные артефакты и capability contracts
  → подготовленная база знаний и Context Profiles
  → handlers, code steps, prompts и tools
  → tests и versioned eval dataset
  → immutable Agent Version
  → code-first workflow с gates и human review
  → shadow/canary/production
  → feedback и следующая версия
```

Границы ответственности:

| Компонент | За что отвечает |
|---|---|
| База знаний | Источники, ревизии, provenance, доступ, проекция и evidence |
| Capability | Стабильный бизнес-контракт с типизированным входом и выходом |
| Agent Implementation | Поиск разрешённого контекста, интерпретация и создание результата |
| Code step | Детерминированные правила и вычисления |
| Workflow | Порядок шагов, retries, ветвление, ожидание человека и остановка |
| Evaluations | Измеримое доказательство качества версии и безопасность handoff |
| Человек | Уточнение неоднозначностей, редактирование и финальное решение |

## 2. Шаг 1. Зафиксировать бизнес-результат и границы

Начинать следует не с модели или prompt, а с решения, которое должен получить
пользователь. Для КП результат формулируется так:

> Менеджер передаёт запрос клиента и получает редактируемый черновик КП/сметы,
> список найденных аналогов, допущения и вопросы для уточнения. Каждая значимая
> позиция имеет источник, суммы воспроизводимы, отправку клиенту подтверждает
> человек.

До написания handler нужно определить:

- пользователя и инициирующее событие;
- ожидаемый итоговый artifact;
- обязательные evidence и критерии полноты;
- решения, которые агент вправе предложить, но не принимать;
- условия отказа, уточнения и передачи человеку;
- стоимость ошибки и запрещённые действия;
- метрики пользовательского результата.

Для первого среза генерации КП агент не отправляет документ клиенту, не изменяет
прайс-листы и не придумывает отсутствующие цены. Если источников недостаточно, он
возвращает пробелы и вопросы, а workflow приостанавливается.

## 3. Шаг 2. Спроектировать артефакты

Следующий шаг — описать Pydantic-модели на языке предметной области. Текст
переписки или chat transcript не должен быть единственным состоянием процесса.

Минимальный набор для сквозного примера:

```text
ClientRequest
  ├── customer and project context
  ├── requested outcome
  ├── supplied materials
  └── constraints

RequirementsArtifact
  ├── requirements[]
  ├── ambiguities[]
  ├── missing_information[]
  └── evidence_refs[]

AnaloguesArtifact
  ├── analogues[]
  ├── relevance reasons
  └── evidence_refs[]

ProposalDraft
  ├── sections[]
  ├── estimate
  ├── assumptions[]
  ├── exclusions[]
  ├── pricing_basis[]
  └── evidence_refs[]
```

Также нужны `Estimate`, `EstimateLine`, `Money`, `PricingBasis` и версия шаблона.
Модели денежных значений используют `Decimal`, явную валюту, налоговый режим и
правило округления.

Для каждого artifact необходимо определить:

- schema version и совместимость изменений;
- обязательные поля и инварианты;
- ссылки на upstream artifacts;
- evidence, необходимое для принятия результата;
- какие поля может редактировать менеджер;
- какие различия нужно показывать в version diff.

## 4. Шаг 3. Подготовить знания

База знаний создаётся до настройки prompt, потому что качество агента ограничено
качеством и доступностью его evidence. Для КП источниками могут быть:

- первичные запросы клиентов и приложения к ним;
- ранее согласованные КП и сметы;
- описания услуг и типовые составы работ;
- шаблоны документов;
- утверждённые прайс-листы и правила расчёта;
- правки менеджеров и финальные версии документов.

Путь одного документа:

```text
исходный файл или запись внешней системы
  → immutable SourceRevision
  → parsing со stable locators
  → canonical business objects
  → versioned Cognee projection
  → RetrievalResult через Context Broker
```

PostgreSQL и оригиналы источников остаются каноническим состоянием. Cognee —
перестраиваемая проекция для graph/vector retrieval, а не источник истины.
Историческое КП является evidence и аналогом, но не автоматически правильным
шаблоном: в нём могут быть устаревшие цены, исключения и ошибки.

До подключения агента разработчик проверяет:

- у каждой записи есть `workspace_id`, источник и ревизия;
- страницы, листы, строки или диапазоны символов имеют стабильные locators;
- ACL применяется к конкретным объектам и фрагментам при retrieval;
- изменение и удаление источника создаёт новую ревизию или tombstone;
- проекцию можно перестроить и откатить;
- цены и правила расчёта отличаются от неутверждённых модельных выводов.

Нельзя передавать handler произвольный доступ ко всему графу. Для каждого вида
работы создаётся versioned `ContextProfile`, например:

| Context Profile | Что разрешено искать |
|---|---|
| `proposal.requirements.v1` | Запрос клиента, приложения и связанные сообщения |
| `proposal.analogues.v1` | Согласованные исторические КП с допустимой свежестью |
| `proposal.services.v1` | Каталог услуг, составы работ и ограничения |
| `proposal.pricing.v1` | Только утверждённые pricing sources и правила |

Профиль фиксирует разрешённые источники, стратегию retrieval, freshness limits,
relation traversal и token budget. Handler обращается к Context Broker через
предоставленный runtime port/tool и не вызывает `cognee.*` напрямую.

## 5. Шаг 4. Разделить процесс на шаги

Не следует помещать весь процесс подготовки КП в один prompt. Каждый шаг должен
иметь понятный контракт и независимо проверяемый результат.

Начальная декомпозиция:

| Шаг | Тип | Результат |
|---|---|---|
| Нормализовать запрос | `code` | Валидный `ClientRequest` |
| Извлечь требования | `agent` | `RequirementsArtifact` |
| Проверить полноту | `gate` | Pass или список пробелов |
| Уточнить требования | `human` | Дополненный `ClientRequest` |
| Найти аналоги | `agent` | `AnaloguesArtifact` |
| Подобрать состав работ | `agent` | Структура предложения с evidence |
| Рассчитать стоимость | `code` | `Estimate` |
| Собрать черновик | `agent` | `ProposalDraft` |
| Проверить черновик | `gate` | Отчёт о schema/evidence/policy checks |
| Отредактировать и утвердить | `human` | Утверждённая версия или возврат на доработку |

Классификация проста:

- вероятностная интерпретация или генерация — `agent`;
- точное преобразование, деньги, налоги и округление — `code`;
- проверка контракта, evidence или policy — `gate`;
- неоднозначное или ответственное решение — `human`;
- долгие retries, timers, approvals и восстановление — ответственность workflow.

Первая реализация может начинаться с более короткой цепочки:

```text
ClientRequest
  → normalize_request (code)
  → extract_requirements (agent)
  → validate_evidence (gate)
  → review_requirements (human)
  → RequirementsArtifact
```

Расширять её следует только после прохождения eval и проверки пользовательского
сценария, а не заранее строить универсальную систему генерации документов.

## 6. Шаг 5. Определить capabilities

`Capability` описывает вид бизнес-работы, а не конкретный prompt или Python-класс.
Для КП возможны следующие контракты:

```text
extract_proposal_requirements:
  ClientRequest → RequirementsArtifact

find_proposal_analogues:
  RequirementsArtifact → AnaloguesArtifact

compose_proposal_structure:
  RequirementsArtifact + AnaloguesArtifact → ProposalStructure

generate_proposal_draft:
  ProposalStructure + Estimate → ProposalDraft
```

Имя capability должно оставаться стабильным при замене модели, prompt или
реализации. Если два шага имеют разные входы, выходы или критерии качества, их не
следует прятать за универсальным строковым router.

Для каждого capability фиксируются:

- input/output schemas;
- допустимые Context Profiles и tools;
- обязательные evidence;
- ограничения времени, стоимости и размера контекста;
- поведение при недостатке данных;
- eval suite и пороги выпуска.

## 7. Шаг 6. Создать Agent Package

Локальные агенты разрабатываются как код в монорепозитории. Рекомендуемая
структура пакета для примера:

```text
src/spine/agents/packages/proposal_generation/
  spine-agent.yaml
  AGENT.md
  handler.py
  schemas.py
  prompts/
    extract_requirements_v1.md
    compose_draft_v1.md
  skills/
  tests/
  evals/
```

Manifest декларативно связывает package с реализацией, но не содержит
исполняемые import paths из базы данных. Его минимальное содержание:

```yaml
package: proposal_generation
version: 1.0.0
implementation_key: proposal_generation.local.v1
capabilities:
  - capability: extract_proposal_requirements
    handler: extract_requirements
    input_schema: ClientRequest.v1
    output_schema: RequirementsArtifact.v1
context_profiles:
  - proposal.requirements.v1
tools: []
resource_limits:
  timeout_seconds: 60
evaluation_suite: proposal_requirements.v1
```

Это иллюстрация целевого manifest; его точная schema должна быть реализована и
версионирована вместе с package loader.

`implementation_key` регистрируется явно в runtime registry. Автоматическое
сканирование модулей, загрузка произвольного Python path из Postgres и выполнение
файлов из package запрещены.

## 8. Шаг 7. Реализовать handler

Handler получает типизированные `AgentRequest` и `AgentExecutionContext`, а
возвращает типизированный `AgentResponse`. Он не сохраняет artifacts сам и не
управляет долговременным состоянием.

Концептуальная форма реализации:

```python
async def extract_requirements(
    request: AgentRequest[ClientRequest],
    context: AgentExecutionContext,
) -> AgentResponse[RequirementsArtifact]:
    retrieval = await context.retrieve(
        query=build_requirements_query(request.input),
        profile="proposal.requirements.v1",
    )

    if not has_sufficient_evidence(retrieval):
        return AgentResponse.abstained(
            output=build_missing_information(retrieval),
            evidence_refs=retrieval.references,
        )

    result = await model.generate_structured(
        schema=RequirementsArtifact,
        prompt=load_prompt("extract_requirements_v1"),
        input=request.input,
        context=retrieval,
    )

    return AgentResponse.completed(
        output=result,
        evidence_refs=collect_used_references(result, retrieval),
    )
```

Этот фрагмент показывает желаемые зависимости, но не является обещанием
существующего API. Конкретный retrieval port должен быть частью контролируемого
runtime context или типизированным Spine tool над Context Broker.

Обязательные свойства handler:

- не импортирует Cognee, Temporal или FastAPI;
- не читает credentials и глобальную конфигурацию напрямую;
- не выбирает произвольные datasets или tools;
- поддерживает cooperative cancellation и deadline;
- не создаёт собственный durable retry loop;
- валидирует структурированный вывод модели;
- возвращает ссылки только на реально полученное и использованное evidence;
- явно abstain, если доказательств недостаточно;
- не раскрывает chain-of-thought в событиях или результате.

Prompts хранятся рядом с реализацией, имеют версии и проверяются тем же eval
suite. Prompt должен объяснять задачу, schema результата, правила использования
evidence и условия отказа. Бизнес-правила, которые можно выразить кодом, не
следует дублировать только в prompt.

## 9. Шаг 8. Вынести точные правила в code steps

Модель может предложить состав работ или определить подходящий `PricingBasis`, но
не должна вычислять итоговую стоимость. Расчёт выполняет типизированная функция с
детерминированными тестами:

```text
EstimateLine(quantity, unit_price, currency, tax_rule)
  → subtotal
  → discount policy
  → tax in defined order
  → explicit rounding
  → total
```

Code step принимает только проверенные входы, возвращает versioned artifact и
сохраняет ссылку на pricing source. Повторный запуск с теми же данными должен
давать тот же результат. Неизвестная цена приводит к ошибке/уточнению, а не к
догадке модели.

## 10. Шаг 9. Сначала собрать eval-набор

До настройки модели и prompt нужно подготовить небольшой репрезентативный набор
обезличенных или синтетических примеров. Реальные клиентские документы нельзя
коммитить без анонимизации.

Для `extract_proposal_requirements` один eval case содержит:

- версию входного `ClientRequest`;
- доступные `SourceRevision` или подготовленный retrieval fixture;
- ожидаемые обязательные требования;
- допустимые варианты формулировок;
- обязательные и запрещённые evidence refs;
- ожидаемые пробелы/уточняющие вопросы;
- версию dataset и assumptions разметки.

Нужны как минимум следующие классы примеров:

- полный и однозначный запрос;
- неполный запрос, требующий уточнения;
- противоречащие друг другу материалы;
- устаревший исторический аналог;
- отсутствие подходящего аналога;
- источник без прав доступа;
- попытка подменить цену текстом из неутверждённого документа;
- две рабочие области с похожими данными для проверки изоляции.

Проверки делятся на три группы:

1. Детерминированные: schema, обязательные поля, деньги, ACL, locator validity,
   отсутствие выдуманных ссылок.
2. Семантические: полнота требований, релевантность аналогов, качество структуры.
3. Human-labelled: полезность для менеджера, существенность правок и пригодность
   черновика.

LLM-as-a-judge может быть дополнительным сигналом, но не заменяет точные проверки
и размеченные людьми примеры.

## 11. Шаг 10. Написать тесты

Тестировать нужно публичный контракт capability и поведение, а не внутренний
порядок вызовов.

Для каждого handler нужны:

- unit tests чистых преобразований и validators;
- application/runtime tests с fake Context Broker и fake model client;
- contract tests manifest, schemas, tools и retrieval boundary;
- eval regression на versioned dataset;
- проверка workspace isolation и отсутствия утечки текста;
- проверка missing/stale/inaccessible/fabricated evidence;
- проверка abstention;
- проверка timeout и cancellation.

Для code steps расчёта добавляются точные проверки `Decimal`, валюты, порядка
налогообложения и округления на граничных значениях. Для workflow используются
Temporal time-skipping и fake activities; проверяются retries, signals,
cancellation, human wait и повторное выполнение activity.

Во время разработки сначала запускается узкий тест изменяемого поведения, затем
репозиторные gates:

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m compileall -q src tests
```

Реальные Cognee и model provider остаются отдельными opt-in integration tests и
не являются условием offline suite.

## 12. Шаг 11. Выпустить immutable Agent Version

Материальное изменение handler, prompt, model profile, Context Profile, tool
grants или output semantics создаёт новую версию. Рабочая версия должна
закреплять:

- package checksum и `implementation_key`;
- capability contracts и schema versions;
- prompt и model profile;
- Context Profiles и projection compatibility;
- tools, grants и resource limits;
- eval dataset, evaluators, метрики и пороги;
- владельца, risk tier и rollback target.

Публикация блокируется, если не пройдены schema compatibility, regression eval,
security/tool review, cost budget или quality threshold. Версия после публикации
не редактируется: исправление выпускается новой версией.

`Agent Binding` выбирает, какая версия выполняет capability в конкретных
workspace, environment или workflow scope. Workflow ссылается на capability, а
не импортирует handler и не зашивает конкретную модель.

## 13. Шаг 12. Собрать code-first workflow

Workflow definitions на текущем этапе создаются и рецензируются как
типизированный код в Git. Control Plane может показывать graph, diff, runs,
deploy и rollback, но не является визуальным редактором исходного определения.

Рекомендуемое разделение:

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

Business workflow definition описывает типизированные steps, transitions,
policies и bindings к capabilities/code-step keys. Temporal adapter переводит
его в durable execution. В определении не должно быть import path из базы,
прямого вызова Cognee или недетерминированного I/O.

Для генерации КП workflow должен явно содержать:

- создание и версии входных artifacts;
- capability steps и их handoff gates;
- ветку уточнения при неполных требованиях;
- детерминированный расчёт сметы;
- human edit/approve;
- запрет отправки без approval;
- idempotency keys, retry/fallback policy и terminal states;
- закреплённую версию определения или полный execution snapshot.

## 14. Шаг 13. Проверить процесс целиком

Перед выпуском запускается вертикальный сценарий:

```text
загрузить тестовые источники
  → построить projection
  → создать ClientRequest
  → запустить workflow
  → проверить промежуточные artifacts и evidence
  → запросить уточнение или approval
  → получить ProposalDraft и Estimate
  → сравнить исходный draft с правками менеджера
```

Проверяются не только финальный текст, но и:

- версии source, projection, agent, prompt, context и workflow;
- lineage каждого промежуточного artifact;
- корректность и доступность citations;
- воспроизводимость суммы;
- отсутствие внешнего действия до approval;
- понятные loading/partial/degraded/failure состояния в UI;
- единый trace от пользовательского действия до retrieval и model call.

## 15. Шаг 14. Развернуть и наблюдать

Жизненный цикл версии:

```text
draft
  → offline eval
  → shadow
  → canary binding
  → production binding
  → continuous handoff eval
  → rollback или superseded
```

В shadow-режиме новая версия обрабатывает копию разрешённого входа, но её
результат не влияет на пользователя. Canary ограничивает новый binding частью
разрешённого scope. Переход в production выполняется только после сравнения
качества, стоимости, latency и человеческих правок с предыдущей версией.

Наблюдаемость должна отвечать на вопросы:

- какая версия и с какими настройками создала результат;
- какие источники и retrieval strategy использовались;
- какой gate остановил или пропустил artifact;
- сколько менеджер исправил и почему отклонил результат;
- можно ли отключить binding или grant и вернуться к прошлой версии.

## 16. Шаг 15. Вернуть feedback в разработку

Правки менеджера, отклонения, уточняющие вопросы и production failures не
переписывают prompt автоматически. Они сохраняются как feedback с lineage,
анонимизируются и после review превращаются в новые eval cases.

Цикл улучшения:

```text
production observation
  → подтверждённая проблема
  → новый или обновлённый eval case
  → изменение кода/prompt/context profile
  → regression comparison
  → новая immutable Agent Version
```

Так база знаний, реализация агента и eval dataset развиваются независимо и
контролируемо. Изменение документа не меняет код агента, изменение prompt не
переписывает историю источников, а feedback не попадает в production без review.

## 17. Definition of Done для нового агента

Агент готов к включению в workflow, когда:

- бизнес-результат и запрещённые действия явно описаны;
- capability имеет versioned input/output schemas;
- handler зарегистрирован явным `implementation_key`;
- доступ к знаниям идёт только через Context Broker и versioned Context Profile;
- каждый значимый вывод имеет доступное evidence или явный abstention;
- deterministic rules вынесены в code steps;
- package, prompts, tools и resource limits версионированы;
- unit, contract, isolation и failure tests проходят;
- versioned eval dataset проходит согласованные thresholds;
- code-first workflow содержит gates, human steps и rollback behavior;
- Agent Version immutable и подключается через scoped binding;
- runs сохраняют lineage, versions, metrics и trace;
- UI позволяет человеку проверить evidence, исправить artifact и принять решение.

Если хотя бы один пункт нельзя подтвердить, агент остаётся draft или работает в
shadow-режиме, но не становится production binding.
