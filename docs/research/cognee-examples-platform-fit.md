# Насколько примеры Cognee подходят архитектуре Spine

Исследование выполнено 13 сентября 2026 года по ветке `main` официального
репозитория Cognee, commit
[`c0d18c8`](https://github.com/topoteretes/cognee/tree/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples).
Рассматривались все примеры в `examples/`, а для MCP — официальный
`cognee-mcp`. Это снимок API и поведения, а не рекомендация фиксировать Spine
на данном commit.

## Краткий вывод

Не все примеры вписываются в Spine напрямую. Почти все они являются
самодостаточными SDK-demo: один процесс очищает хранилища, загружает данные,
строит граф, вызывает LLM, печатает ответ и иногда рисует HTML. Для учебного
скрипта это удобно, но в Spine эти обязанности разделены между connector и
ingestion pipeline, Cognee projection-worker, Context Broker, agent runtime,
Temporal workflow, evaluation и web-интерфейсом.

Полезна значительная часть **возможностей** Cognee: `remember`, типизированные
`DataPoint`, ontology, разные retrieval strategies, temporal retrieval,
incremental DLT ingestion, ACL, provenance graph и bounded visualization. Но
публичный код Spine не должен повторять прямые вызовы `cognee.*`, глобальный
`forget/prune`, использование `node_set` как security boundary, выдачу
`GRAPH_COMPLETION` как готового ответа, автоматическое обучение по feedback или
встроенную в Cognee оркестрацию агентов.

Индекс Cognee утверждает, что каталог содержит 64 Python-скрипта в трёх
папках, однако на исследованном commit фактически находится 70 `.py`-файлов в
`guides`, `advanced_guides`, `demos` и `integrations`. Поэтому ниже используется
фактическое дерево, а не только таблица из
[`examples/README.md`](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/README.md).

## Классификация

- **A — Context Broker / Cognee adapter:** capability уместна за типизированным
  retrieval-контрактом Spine.
- **B — ingestion / projection:** уместна только в connector, ingestion или
  projection-worker, а не в API, workflow или agent implementation.
- **C — деталь реализации агента:** может использоваться внутри локального
  `AgentHandler`, но не определяет платформенный agent protocol.
- **D — workflow / evaluation / operations:** ответственность Spine вне
  Cognee.
- **E — небезопасно без изменений:** пример нарушает хотя бы один инвариант
  Spine и не должен переноситься буквально.

Одна группа может иметь несколько меток: например, retrieval capability
относится к A, а способ её вызова в demo — к E.

## Инвентарь по архитектурному поведению

| Группа | Что показывают примеры | Место в Spine | Решение |
|---|---|---|---|
| Базовая память и document Q&A | `simple_cognee_example`, `recall_core`, `simple_document_qa`, `remember_recall_improve`, `company_brain` и comprehensive demo совмещают загрузку, построение проекции и готовый LLM-ответ | A, B, E | **Адаптировать:** ingestion и retrieval разделить; наружу возвращать `RetrievalResult`, а ответ строить capability `answer_question` |
| Retrieval strategies | `CHUNKS`, hybrid lanes, `GRAPH_COMPLETION`, global-context index, temporal, importance/truth reranking, references и фильтрация по node set | A, E | **Принять как набор стратегий adapter**, но выбирать их только через versioned `ContextProfile`; completion не считать ответом Spine |
| Session memory и distillation | `sessions`, conversation persistence, session distillation, session-flow, live feedback, feedback lifecycle и agent trace context | C, D, E | **Отложить:** допустим bounded conversational cache; durable workflow state остаётся в Temporal, feedback и learnings — в Postgres/eval datasets |
| Структурированный граф | custom graph models, `DataPoint`, ontology, prompts, entity consolidation, fact validity, description consolidation | B, E | **Принять выборочно:** собственные versioned projection models; LLM-выводы не смешивать с фактами, canonical identity не менять эвристическим merge |
| Custom Cognee tasks/pipelines | `custom_tasks_and_pipelines`, custom cognify, single-object pipeline, dynamic HR stages, org hierarchy, coding-rule memify | B, D, E | **Адаптировать только внутри projection-worker:** Cognee pipeline не заменяет Temporal и не определяет бизнес-workflow |
| Источники и миграции | URL, S3, audio/image/OCR, DLT, relational DB, Mem0, Letta/Zep/Graphiti, community connectors | B, E | **Адаптировать:** сначала immutable raw payload и `SourceRevision`, затем canonical object и projection; loader Cognee не становится connector contract |
| Code graph | deterministic Enola pipeline и `SearchType.CODE`: facts, paths, impact, diagrams, delta | A, B, E | **Отложить:** хорошая отдельная projection/retrieval capability, но она не нужна первым четырём vertical slices и устанавливает внешний binary |
| Agent memory decorator | `@cognee.agent_memory` оборачивает произвольную функцию, сохраняет session traces и затем переносит их в graph | C, E | **Не использовать как runtime seam:** Spine уже выбрал `AgentHandler`, stateless implementation и runtime-owned traces; ограниченный use внутри handler возможен лишь без скрытого durable state |
| Agentic reasoning и skills | procurement demo вручную выполняет research-then-decide; `AGENTIC_COMPLETION` выбирает skills/tools; skill feedback demo автоматически применяет улучшение | C, D, E | **Прототипировать, не переносить:** шаги, artifacts, evidence, approvals и evaluations должны быть видимы Spine; произвольный tool selection и auto-apply запрещены |
| Permissions и handover | users, tenants, roles, dataset ACL; shared dataset доступен другому пользователю по UUID; supervisor/worker обмениваются памятью | A, D, E | **Использовать как defense in depth:** источник решения — Spine RBAC/ABAC и `workspace_id`; Cognee identity/dataset mapping принадлежит adapter |
| Feedback, contradictions и self-improvement | rating меняет retrieval weight; contradiction edges сохраняют обе версии; correction добавляется как новый текст | A, B, D, E | **Экспериментировать за versioned flag:** feedback хранить канонически и прогонять offline eval; вес не устанавливает истинность и не заменяет revisions/tombstones |
| Visualization и provenance | bounded/full graph, query- и recall-seeded view, semantic map, schema inventory, ownership/provenance graph | A, D, E | **Перенять UI-идеи:** Context Explorer получает server-side filtered data; готовый HTML и full-graph render не являются product UI и могут раскрыть чужие данные |
| Backends и deployment | local Ollama + Kuzu/LanceDB, Neptune Analytics, S3, Docker sandbox memory | B, D, E | **Local stack принять; production выбрать нагрузочными и ACL-тестами:** Neptune example сейчас непригоден для multi-tenant Spine |
| MCP | официальный Cognee MCP выставляет memory operations напрямую через stdio/SSE/HTTP | D, E | **Не подключать напрямую:** если MCP понадобится, он должен оборачивать Context Broker/Tool Gateway Spine, а не обходить policy и audit |

## Главные архитектурные расхождения

### 1. Прямые вызовы Cognee обходят Context Broker

Примеры намеренно вызывают `cognee.remember`, `recall`, `search`, `improve` и
`forget` напрямую. Например, document Q&A сначала делает `forget(everything=True)`,
затем `remember(file)` и выдаёт результат `recall()` пользователю
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/advanced_guides/simple_document_qa/simple_document_qa_demo.py)).
В Spine это нарушает границу [Context Broker](../ARCHITECTURE.md#retrieval-contract):
API, workflow и agent handler не должны знать API Cognee.

Правильное разделение:

```text
SourceRevision -> projection-worker -> Cognee adapter
AgentHandler -> Context Broker -> policy -> Cognee adapter
AgentHandler <- typed RetrievalResult <- Context Broker
```

### 2. Cognee completion нельзя выдавать как платформенный ответ

`GRAPH_COMPLETION`, `RAG_COMPLETION` и `HYBRID_COMPLETION` соединяют retrieval и
LLM generation. При этом в Spine Context Broker обязан вернуть chunks, entities,
relations, references, strategy и index version, а отдельный versioned
`answer_question` handler — сгенерировать и проверить ответ.

Для адаптера особенно полезны режим `only_context=True` и настройка отдельных
lanes hybrid retrieval, показанные в
[`hybrid_retrieval_recall.py`](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/hybrid_retrieval_recall.py).
Completion можно оставить внутренней экспериментальной стратегией, но её вывод
всё равно должен быть разобран в типизированный result и пройти Q&A evaluations.

### 3. Встроенных references недостаточно для citations Spine

`include_references=True` дописывает Evidence прямо в строку ответа. Сам пример
предупреждает, что references пропускаются для structured output и могут тихо
исчезнуть при ошибке backend
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/references_example.py)).
Это противоречит exit criterion R1: ответ нельзя выдать без ссылки на доступную
конкретную ревизию.

Adapter должен получать provenance/used graph element IDs, сопоставлять их со
`SourceRevision` и стабильным locator (page, sheet/row, message ID, character
range), а отсутствие ссылок возвращать как typed degraded/error outcome, не как
обычную строку.

### 4. Dataset и node set решают разные задачи

Procurement demo делит vendor conversations, purchase history и policies через
`node_set`, а затем делает scoped recall по `node_name`
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/demos/agentic/agentic_reasoning_procurement_example.py)).
Это полезно как retrieval taxonomy, но не как изоляция. В Spine dataset создаётся
на `workspace + security domain`, а node set обозначает source type, project или
другой search scope.

Cognee ACL действительно запрещает чтение чужого dataset и требует dataset UUID
для разрешённого cross-user доступа
([ACL example](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/demos/permissions/data_access_control_example.py)).
Это стоит проверять contract tests, но identity Cognee не заменяет
`workspace_id`, acting subject, purpose policy и object-level ACL Spine.

### 5. Custom pipeline — projection mechanism, не business workflow

Cognee позволяет собрать `Task` pipeline, вызвать LLM structured extraction и
сохранить `DataPoint` в graph/vector stores
([пример](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/demos/custom_pipelines/custom_pipeline_single_object_example.py)).
Это хороший extension seam внутри adapter. Но dynamic HR example просто включает
стадии boolean-флагами, а custom cognify повторяет внутренние SDK tasks. Здесь нет
durable timers, idempotent Activities, approvals, handoff contracts или recovery.
Такие pipeline не должны конкурировать с Temporal.

Direct `LLMGateway` из `low_level_llm.py` также не является Context Broker
capability: это зависимость agent implementation/model adapter, которую нужно
версионировать и трассировать через agent runtime.

### 6. Источники должны пройти через canonical ingestion

DLT и community connector patterns полезны: `merge` по primary key, `replace`
для snapshot и orphan cleanup при upstream deletion
([официальное описание](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/integrations/README.md)).
Но перед `remember()` Spine обязан сохранить immutable raw payload, checksum,
`SourceObject`/`SourceRevision`, cursor и canonical projection. Физическое
удаление orphan из Cognee совместимо только потому, что история и tombstone
остаются в Postgres/raw storage.

Аналогично, `fact_validity.py` закрывает устаревший graph node через `valid_to`
вместо удаления
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/fact_validity.py)).
Это полезная форма projection, но не источник истории факта.

### 7. Session memory не должна стать вторым durable runtime

`@cognee.agent_memory` хранит session traces и после порога переносит их в
knowledge graph
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/agent_memory_quickstart.py)).
Другие demos извлекают guidance из разговоров/tool traces и distill их в
permanent memory. Это конфликтует со stateless `AgentHandler`, runtime-owned
events и правилом, что durability принадлежит Temporal.

Допустимый вариант позже: session cache хранит только bounded conversational
context, а отдельный versioned workflow предлагает learnings. Перед публикацией
они сохраняются как probabilistic conclusions с evidence, проходят policy/eval
и лишь затем проецируются в Cognee. Автоматическое превращение произвольной
реплики или tool error в постоянное правило неприемлемо.

### 8. Feedback улучшает ranking, но не устанавливает истину

Contradiction demo хорошо показывает важное ограничение: высокий rating
поднимает веса всех использованных элементов, но не разрешает конфликт; ответ
меняет новая корректирующая запись
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/demos/feedback/contradiction_feedback_demo.py)).
Это согласуется с разделением фактов и выводов Spine.

Feedback signal нужно хранить в Postgres вместе с run, agent/context/index
versions и human label. Применение к reranking — только новая версия projection,
с offline replay, regression thresholds и rollback. `skill_feedback_loop`
применяет LLM-предложение к skill автоматически; в Spine такой auto-apply
следует отклонить, поскольку он обходит immutable agent versions и release gates.

### 9. Очистка в demo разрушительна для shared platform

Большинство примеров начинают с `forget(everything=True)` либо пары
`prune_data()` / `prune_system(metadata=True)`. Neptune guide отдельно
предупреждает, что финальный `forget(everything=True)` очищает настроенный cloud
graph
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/neptune_analytics_example.py)).

В production-коде Spine такие операции запрещены. Нужны workspace/domain-scoped
delete, tombstone, idempotency key, audit, retention policy и отдельный rebuild
workflow с shadow index. Полный prune допустим только в изолированном test
fixture или одноразовой local environment.

### 10. Backends требуют проверки совместимости с ACL

Local Ollama example использует Kuzu + LanceDB + SQLite, что близко к local
плану Spine
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/local_ollama_example.py)).
Code graph тоже указывает, что отдельные datasets ищутся независимо и не дают
cross-dataset paths
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/code_graph_example.py)).

Docker sandbox guide перечисляет backends с per-user/dataset isolation и прямо
отмечает отсутствие поддержки для Neptune, Ladybug Remote и Neptune Analytics
([официальный guide](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/integrations/docker-sandbox-kit/README.md)).
Поэтому Neptune нельзя выбирать для multi-tenant production Spine только по
наличию примера. Neo4j/pgvector либо другой stack должен пройти concurrency,
ACL, deletion, backup/restore и rebuild contract tests.

### 11. MCP не должен открывать Cognee в обход Spine

MCP находится не среди Python examples, а в отдельном официальном package.
Сервер выставляет `remember`, `recall` и `forget` через stdio/SSE/HTTP; direct
mode импортирует Cognee локально, API mode проксирует Cognee API
([`cognee-mcp` README](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/cognee-mcp/README.md)).
Прямой grant такого MCP даст агенту mutating memory surface и обойдёт typed
Context Broker result, Spine policy, provenance mapping и audit.

Если MCP станет нужен, Spine должен предоставить собственный read-oriented MCP
tool над Context Broker с `AgentExecutionContext`, ограниченным ContextProfile и
typed references. Mutating ingestion/delete остаются отдельными policy-checked
commands. Для первых четырёх кейсов этот MCP можно отложить.

### 12. Visualization — UI reference, не готовый интерфейс

`visualize_graph` умеет строить bounded subgraph, выбирать seeds по query или
provenance конкретного recall и отдельно предлагает опасный full render
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/graph_visualization.py)).
`memory_provenance` показывает ownership chain dataset → document → extracted
memory
([исходник](https://github.com/topoteretes/cognee/blob/c0d18c80e24b7b78918e7642c03f6f128fdd2aee/examples/guides/memory_provenance.py)).

Эти идеи подходят Context Explorer и citation inspector. Однако браузер не
должен получать Cognee HTML или обращаться к графу напрямую: API сначала
применяет workspace/object ACL, затем возвращает bounded DTO с source revisions.
Примеры также не дают Spine observability: `print`, JSON-файл, HTML и простой
`PASSED/FAILED` smoke verdict не заменяют единый `trace_id`, OpenTelemetry,
versioned eval dataset и метрики handoff/outcome.

## Решение по четырём приоритетным кейсам

### R1 — Q&A по внутренним документам

**Принять сейчас:**

- `remember` как внутреннюю реализацию projection command;
- chunk, graph-context и hybrid retrieval с `only_context=True`;
- dataset ACL как дополнительную защиту;
- bounded provenance view для citation inspector;
- Kuzu/local vector store для разработки.

**Обязательно адаптировать:** stable locators и `SourceRevision`, typed
`RetrievalResult`, abstention, object ACL, index version, controlled delete/rebuild
и regression eval. Встроенный Evidence block и готовый completion недостаточны.

### R2 — генерация КП

**Принять после R1:** custom `DataPoint`, ontology и hybrid retrieval для
`ClientRequest`, requirements, analogues и pricing basis. Procurement demo полезен
как черновой сценарий research → decision, но его строки QA и финальный свободный
LLM-вызов нужно разложить на typed workflow steps, evidence-bearing artifacts,
completeness gate и human approval. Деньги, налоги и округление остаются
детерминированным code step.

### R3 — мониторинг коммуникаций

**Принять после R2:** DLT incremental connector patterns, message node sets как
retrieval scope, temporal extraction, contradiction representation и source
deletion reconciliation. Detectors, dedup/cooldown, historical replay, finding
lifecycle и notification idempotency находятся вне Cognee. Feedback weights не
могут закрывать finding или менять canonical fact.

### R4 — контроль задач

**Использовать осторожно:** temporal context и graph relations помогают найти
зависимости и историю обязательств. Session/tool-trace learnings могут стать
экспериментальным сигналом позже. Состояние задачи, deadline, owner, retry,
approval и status transition остаются в Postgres/Temporal; graph completion не
определяет, выполнена ли задача.

### Отложить вне четырёх кейсов

- code graph и coding-agent rules;
- миграции Mem0/Letta/Zep/Graphiti без реального источника данных Spine;
- direct Cognee MCP;
- cross-sandbox agent memory handover;
- truth-centroid tuning и automatic skill improvement;
- Neptune Analytics deployment.

## Рекомендуемый минимальный spike для R1

1. Реализовать в Cognee adapter отдельные `project(SourceRevision)` и
   `retrieve(ContextQuery) -> RetrievalResult`, не универсальный pass-through.
2. Создавать dataset только через deterministic mapping
   `workspace_id + security_domain`; node set использовать только как filter.
3. Сравнить `CHUNKS`, graph context и balanced hybrid на одном versioned golden
   dataset, сохраняя raw candidates, latency и index version.
4. Построить собственные references из canonical locator metadata и проверить
   ACL leakage/citation correctness; не парсить Evidence из answer string.
5. Проверить update, tombstone, workspace-scoped delete и полный shadow rebuild
   на Kuzu/local vector backend.
6. Выполнить contract tests Cognee ACL как defense in depth, включая denied
   cross-workspace retrieval.

Итоговый критерий принятия Cognee-примера в Spine: он помогает реализовать
текущий capability или projection, оставаясь за adapter boundary и сохраняя
workspace isolation, provenance, versioning, idempotency, evaluation и
наблюдаемость. Сам факт, что сценарий запускается end-to-end одним скриптом, не
делает его платформенным шаблоном.
