# Cognee 1.5.4 и контракт Context projection Spine

Исследование выполнено 7 октября 2026 года по официальному release
[`v1.5.4`](https://github.com/topoteretes/cognee/releases/tag/v1.5.4), commit
[`20e0bd8`](https://github.com/topoteretes/cognee/commit/20e0bd88746de2d96e99b4b122361dfc3dad21bc).
Все выводы о поведении ниже относятся именно к этой версии. Публичными
interface считаются экспортированные SDK-функции и HTTP routes; внутренние
модели и helpers использованы только как доказательство реализации и не должны
импортироваться adapter'ом Spine.

## Примечание об интеграции с принятой архитектурой

Исходные выводы ниже сохранены как результат исследования Cognee 1.5.4. При
интеграции 8 октября 2026 года они были сверены с принятыми
[ADR 0001](../adr/0001-canonical-store-and-context-graph.md),
[ADR 0011](../adr/0011-canonical-document-access-policy.md) и
[ADR 0018](../adr/0018-canonical-source-and-projection-publication-lifecycle.md).

ADR 0001 подтверждает роль Cognee как заменяемого projection engine за
Spine-owned interface. ADR 0011 подтверждает, что Cognee dataset permissions
остаются только defense in depth, а единственным источником полномочий является
текущий `AccessPolicy` в PostgreSQL с повторной проверкой каждого evidence.

ADR 0018 уточняет lifecycle-термины, принятые уже после исследования. Упомянутый
ниже `ProjectionVersion` теперь разделён на immutable processing definition
`ProjectionConfigVersion` и immutable logical publication manifest
`ProjectionSnapshot`. Формулировки исследования о возврате к предыдущей active
version описывают отсутствующий в Cognee механизм, но не разрешённый Spine
rollback protocol: Spine никогда не переводит active pointer на старый snapshot,
а публикует successor snapshot на неубывающей canonical boundary. PostgreSQL
остаётся authority для compare-and-swap activation; Cognee aliases, datasets и
physical stores являются только производным состоянием. Эти уточнения не меняют
вывод о необходимости Spine-owned shadow lifecycle и bounded prototype.

## Краткий вывод

Cognee 1.5.4 подходит как глубокий engine за `ContextProjection`, но не реализует
контракт Spine самостоятельно. Публичные `add`, `cognify`, `search`, `update`,
`datasets.delete_data` и `forget` дают необходимые механизмы ingestion,
ontology-aware graph extraction, graph/vector retrieval и очистки. Их следует
закрыть adapter'ом, который владеет сопоставлением `SourceRevision` с Cognee
`data_id`/chunk IDs, формирует типизированные results и проверяет policy.

Обязательные свойства Spine, которых в публичном contract Cognee нет:

- immutable `SourceRevision` и page/sheet/row/character locator;
- `ProjectionVersion`, idempotency key и полный диагностический receipt;
- атомарная активация shadow projection и rollback на предыдущую active version;
- durable cancel-by-run-id;
- workspace/environment/object-level authorization и проверка каждого evidence;
- гарантия, что provenance или citations не исчезнут при внутренней ошибке.

Поэтому решение для R1: **сохранить Cognee 1.5.4 как projection engine, но не
вызывать его напрямую из API/workflow/agent code**. Сначала нужен bounded
prototype adapter и contract suite. Если выбранная пара backend'ов не проходит
provenance/delete/isolation/shadow gates, требуется owned fallback для этой
части, а не обход публичных interfaces Cognee.

## Матрица соответствия

| Требование Spine | Публичный interface Cognee 1.5.4 | Решение | Ограничение |
|---|---|---|---|
| Metadata и source identity | `add(data, dataset_name, node_set, ...)`; `DataItem.external_metadata` | **Wrapper** | Metadata Cognee не является immutable revision; adapter сначала сохраняет `SourceRevision`, затем передаёт только projection metadata |
| Source → chunk mapping | `cognify`; `search(CHUNKS, verbose=True, include_references=True)` | **Wrapper + prototype** | Есть `data_id`, chunk ID/index/hash, но нет стабильных page/sheet/row/character locators |
| Ontology-constrained extraction | `cognify(graph_model=..., config={ontology_config: ...})` | **Prototype** | Ontology применяется после LLM extraction; strict mode ограничивает nodes, но не полностью валидирует relations и не фильтрует chunk retrieval |
| Scoped retrieval | `search(datasets/dataset_ids, node_name, query_type, only_context, verbose, include_references)` | **Wrapper** | Dataset ACL — defense in depth; `dataset` и `node_set` не являются authorization Spine |
| Update | `update(data_id, data, dataset_id, chunk_level_diff=True)` | **Prototype** | Incremental path staged, но fallback удаляет live representation перед успешным rebuild |
| Delete | `datasets.delete_data`; `forget(..., memory_only=True)` | **Prototype** | Cleanup распределён между relational/graph/vector/session/evidence stores; отдельные шаги best effort |
| Full rebuild | `forget(dataset, memory_only=True)` + `cognify(dataset)` | **Wrapper + prototype** | Это in-place rebuild, не shadow version и не atomic activation |
| Shadow isolation / activation | отдельный dataset/backend namespace можно создать обычными calls | **Owned lifecycle** | First-class shadow projection, active pointer и atomic swap не найдены |
| Backends | relational SQLite/Postgres/Turso; graph Ladybug/Kuzu/Neo4j/Neptune/Turso/Postgres demo; vector LanceDB/pgvector/Neptune/Turso | **Prototype per pair** | Возможности ACL, provenance, delete и concurrency различаются; Postgres graph помечен demo |
| Cancellation | cooperative `CancelledError` внутри pipeline | **Owned fallback** | Нет публичного durable cancel-by-`pipeline_run_id`; background tasks и queues process-local |
| Diagnostic receipt | `PipelineRunInfo`, dataset status/progress, activity rows | **Wrapper + owned manifest** | Ack/status не содержит source map, projection/ontology/config/model versions, idempotency key и activation history |

## Metadata, provenance и source-to-chunk mapping

### Что есть в 1.5.4

`Data` имеет dataset-scoped identity и хранит `owner_id`, `tenant_id`,
`dataset_id`, hashes исходного и обработанного content, пользовательский
`external_metadata`, системный `system_metadata`, `node_set`, pipeline status и
timestamps ([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/data/models/Data.py#L9-L61)).
Ingestion разделяет пользовательскую metadata и служебную metadata; для file/URL
origin записывается source URI, тогда как plain text собственного URI не имеет
([DataItem](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/ingestion/data_item.py#L14-L22),
[ingestion](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/ingestion/ingest_data.py#L320-L419)).

`DocumentChunk` сохраняет `is_part_of`, `chunk_index`, content hash,
`document_id` и `document_name`
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/chunking/models/DocumentChunk.py#L12-L60)).
Text chunk ID выводится из document ID, hash текста и occurrence, поэтому
одинаковые bytes можно стабильно сопоставить в рамках документа
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/chunking/TextChunker.py#L12-L58)).
Verbose search с `include_references=True` может вернуть structured evidence с
dataset/data/chunk IDs, chunk index, document name, rank и score
([evidence model](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/search/models/EvidenceReference.py#L1-L41),
[public result conversion](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/search/methods/search.py#L449-L496)).

Внутри Cognee также есть audit provenance ledger с source, location, quote,
hash-chain, temporal/version/invalidation fields
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/provenance/models/ProvenanceEntry.py#L14-L81))
и append-only edge→chunk evidence sidecar
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/provenance/edge_evidence/lookup.py#L1-L131)).
Но audit tracking выключен по умолчанию, а `record_provenance` обрабатывает
сбой как warning, не как failure projection
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/provenance/record_provenance.py#L1-L30)).

### Чего недостаточно для Spine

Cognee provenance не заменяет immutable `SourceRevision`: он живёт в
rebuildable projection и не является обязательным success condition. Кроме
того, `start_index`/`end_index` provenance получают ordinal chunk, а не
character range
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/provenance/record_provenance.py#L192-L220)).
PDF loader передаёт page text generic chunker'у без page metadata; chunker может
объединять input across page boundaries
([PDF](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/data/processing/document_types/PdfDocument.py#L12-L29),
[chunker](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/chunking/TextChunker.py#L29-L110)).

Adapter должен до `add` сохранить canonical revision/checksum/raw object и
собственный locator map. После `cognify` он связывает
`source_revision_id → data_id → chunk_id → stable locator`. Отсутствующая или
несопоставимая reference делает receipt/retrieval degraded или failed; это не
может быть silently omitted citation.

## Ontology и extraction

Публичный `cognify` принимает Pydantic `graph_model` и ontology configuration.
Фактический pipeline сначала извлекает graph через LLM, затем optional resolver
canonicalizes/enriches результат
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/graph/extract_graph_from_data.py#L86-L159)).
RDFLib resolver поддерживает RDF/OWL files и fuzzy matching
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/ontology/rdf_xml/RDFLibOntologyResolver.py#L37-L131)).

`strict` mode сохраняет extracted node, если его type совпал с ontology class
или name — с individual. Он не является полным OWL validator для relation name,
domain/range, cardinality или disjointness
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/ontology/construct_data_points_and_edges_with_ontology.py#L133-L214)).
Pruning относится к graph data points; исходные chunks остаются доступны через
CHUNKS/lexical/RAG lane
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/ontology/construct_data_points_and_edges_with_ontology.py#L217-L262)).

Следствие: adapter может передать published `OntologyVersion`, custom graph
model и prompt, но должен сам закрепить их hashes/versions в projection
manifest. Prototype обязан проверить forbidden entity/relation cases и то, что
невалидное graph extraction не становится будто бы ontology-valid fact. Cognee
не следует использовать для неявной публикации новой production ontology;
discovery сохраняется как `OntologyCandidate` Spine.

## Retrieval scope и authorization

`search` предоставляет необходимые knobs: explicit dataset IDs, `SearchType`,
`top_k`, node-name filter, raw context, verbose objects и structured references
([public signature](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/search/search.py#L41-L74)).
Перед search requested datasets фильтруются через read permissions пользователя,
а отсутствие списка означает все readable datasets
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/search/methods/search.py#L159-L218)).
При включённом backend access control Cognee создаёт per-dataset storage context;
при выключенном search выполняется без такого физического context
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/search/methods/search.py#L359-L425)).

`node_set` materializes organizational graph tags/relationships
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/tasks/documents/classify_documents.py#L68-L103));
это search taxonomy, а не permission gate.

Правило adapter'а без исключений:

1. Context Broker авторизует workspace, environment, acting subject, purpose и
   source/object ACL до вызова Cognee.
2. Dataset выбирается только из server-side mapping
   `workspace + security_domain → physical dataset/version`; caller не передаёт
   произвольный dataset.
3. `node_set` только сужает разрешённый corpus.
4. Все returned references повторно проверяются по current revision и ACL.
5. Dataset ACL Cognee остаётся defense in depth. **Ни dataset, ни `node_set` не
   считаются достаточной authorization boundary.**

Для `RetrievalResult` следует использовать non-generative modes и `verbose=True`:
`CHUNKS` для passages и explicit graph/hybrid retrieval для entities/relations.
Completion string Cognee не становится ответом capability Spine. Public search
также удаляет effective `search_type` из backward-compatible result, поэтому
adapter должен pin strategy, а не полагаться на default hybrid deferral
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/search/methods/search.py#L449-L456)).

## Update, delete, rebuild и shadow isolation

`update` сохраняет `data_id`, пробует chunk-level diff и при неподдержанном
случае переходит на full delete + pinned re-add + cognify
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/update/update.py#L56-L145)).
Incremental path staging сначала записывает новые graph/vector artifacts и
только затем публикует relational row
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/update/incremental.py#L691-L739)).
Это полезный локальный safety mechanism, но не immutable version activation.
Full fallback удаляет live representation до успешного re-add/cognify, поэтому
не годится для обновления active projection без внешней версии
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/update/update.py#L298-L340)).

`datasets.delete_data` проверяет delete permission, берёт dataset lock и удаляет
graph/vector ownership, session references и relational row
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/datasets/datasets.py#L210-L300)).
`forget(memory_only=True)` сохраняет raw row/files, очищает graph/vector/evidence
и reset'ит cognify status, позволяя re-cognify
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/forget/forget.py#L243-L341)).
Failed cognify имеет run-scoped rollback, а startup recovery обрабатывает stale
STARTED runs после threshold
([rollback](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/cognify/rollback.py#L107-L156),
[recovery](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/cognify/recovery.py#L17-L107)).

В 1.5.4 не найден публичный first-class interface для:

- build в immutable shadow projection;
- validate/evaluate перед visibility;
- atomic switch active pointer;
- rollback readers на предыдущую version.

Spine должен реализовать lifecycle в Postgres и строить каждую
`ProjectionVersion` в отдельном physical dataset/database namespace. Context
Broker разрешает только active mapping. `forget` + `cognify` допустимы для
одноразового local rebuild или очистки retired namespace, но не как production
cutover protocol. Конкретную namespace strategy необходимо подтвердить
prototype'ом на выбранной паре graph/vector backends.

## Backend constraints

Встроенные relational providers: SQLite, Postgres и Turso/libSQL
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/infrastructure/databases/relational/create_relational_engine.py#L51-L101)).
Graph factory поддерживает Neo4j, Ladybug/Kuzu local/remote, Neptune и Neptune
Analytics, Turso и `postgres_demo`; последний прямо отмечен как неготовый к
production
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/infrastructure/databases/graph/get_graph_engine.py#L377-L591)).
Vector factory поддерживает LanceDB, pgvector, Neptune Analytics и Turso; иные
adapters регистрируются community packages
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/infrastructure/databases/vector/create_vector_engine.py#L165-L340)).

Support в factory не означает contract parity. Dataset handlers перечисляют
разные совместимые physical isolation strategies
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/infrastructure/databases/dataset_database_handler/supported_dataset_database_handlers.py#L35-L83)).
Для R1 каждая рассматриваемая пара обязана пройти одинаковые tests: two-workspace
isolation, denied evidence leakage = 0, provenance round-trip, repeated delete,
partial-failure recovery, concurrent update/delete, full shadow rebuild,
activation и rollback. До этого ни один backend нельзя назвать production
profile Spine.

## Cancellation и receipts

Background pipeline в 1.5.4 — process-local `asyncio.Task`, удерживаемая в
module set; progress идёт через in-memory queue
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L17-L21),
[runner](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L54-L127)).
Внутренний `CancelledError` запускает rollback/error logging и затем
propagates, но публичного durable cancel-by-run-id interface не найдено
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/pipelines/operations/run_tasks.py#L293-L347)).
Disconnect streaming recall намеренно detach'ит consumer и не отменяет work
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/api/v1/recall/recall_stream.py#L26-L38)).
Отмена принадлежит Temporal Activity/workflow Spine; adapter может cooperative
cancel текущую coroutine, но не обещает durable cancellation после restart.

`PipelineRunInfo` содержит status, run ID, dataset ID/name, payload и ingestion
info; error subtype добавляет scrubbed class/message
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/pipelines/models/PipelineRunInfo.py#L7-L62)).
Durable `PipelineRun` добавляет user/tenant, timestamps, outcome, token usage,
parent/origin/background и error fields
([source](https://github.com/topoteretes/cognee/blob/v1.5.4/cognee/modules/pipelines/models/PipelineRun.py#L27-L83)).
Это useful operational acknowledgement, но не `ProjectionReceipt` Spine.

Spine receipt обязан дополнительно записать:

- `workspace_id`, environment, idempotency key и `projection_version`;
- source revision → Cognee data/chunk/graph refs + stable locators;
- ontology, graph model, prompt, parser/chunker, embedding и Cognee versions;
- physical backend namespaces и configuration hashes;
- counts, warnings, failures, degraded provenance/citation flags;
- eval result, activation decision, prior/active version и timestamps.

Receipt создаётся Spine даже если Cognee call завершился исключением. Повторный
command с тем же idempotency key возвращает тот же logical outcome или безопасно
продолжает recovery; случайный Cognee `pipeline_run_id` не является таким key.

## Текущий prototype Spine

Текущий [`cognee_memory.py`](../../src/spine/memory/cognee_memory.py) передаёт
plain strings в `remember`, возвращает `Any`/raw recall results и не задаёт
explicit strategy, `include_references`, workspace user mapping, projection
version или lifecycle operations. `format_answers` угадывает поля `text` и
`source`; traceability гарантировать нельзя.

[`ingest/loaders.py`](../../src/spine/ingest/loaders.py) встраивает source/type/title
в body text вместо typed metadata. PDF concatenation теряет pages; XLSX locator
присутствует только как часть строки source path. PDF/DOCX extraction exceptions
превращаются в ingestible текст `(failed to extract ...)`, то есть failure может
быть проиндексирован как факт. Это dangerous fallback: loader должен вернуть
structured failed/partial result и не передавать error prose в projection.

[`memory/contracts.py`](../../src/spine/memory/contracts.py) уже задаёт
`workspace_id`, references, retrieval strategy и index version, но текущая
реализация их не заполняет. `ContextReference.locator` пока untyped и не несёт
source revision, freshness/access state или evidence role.

## Рекомендуемый bounded prototype

1. Зафиксировать Cognee adapter только на 1.5.4 и использовать public SDK calls.
2. На synthetic PDF/DOCX/XLSX corpus создать canonical revisions и typed locator
   map до Cognee ingestion.
3. Спроецировать одну published ontology и negative relation/type examples.
4. Вернуть `RetrievalResult` из CHUNKS и graph lanes с validated references, без
   Cognee completion string.
5. Прогнать two-workspace ACL tests и доказать, что caller dataset/node_set не
   расширяют разрешённый corpus.
6. Проверить incremental и full update, tombstone, repeated delete, failure
   recovery и отсутствие stale evidence.
7. Построить новую version в отдельном namespace, выполнить eval/ACL gates,
   переключить active mapping и rollback'нуть readers на предыдущую version.
8. Сравнить минимум local test profile и production candidate
   `Postgres + Neo4j + pgvector`; не принимать `postgres_demo` как production
   graph.

Go decision: public interfaces закрывают semantic engine без импорта Cognee
internals, а выбранная backend pair проходит все gates. No-go/owned fallback:
нужны private imports/patches для provenance, невозможно получить stable source
mapping, есть ACL leakage, либо нельзя изолировать и переключить projection
version без destructive live mutation.

## Новые follow-up tickets и dependencies

Исследование не меняет dependencies. Однако `uv.lock` фиксирует 1.5.4, тогда как
`pyproject.toml` объявляет `cognee>=1.0.0`; это не защищает проверенный contract
от установки другой minor/major version. Нужен отдельный dependency ticket:
закрепить поддерживаемый диапазон вокруг 1.5.4 и описать upgrade gate с rebuild +
retrieval/provenance/ACL regression eval.

Отдельные implementation tickets, выявленные проверкой:

1. typed source-revision/locator map и исправление loader failure semantics;
2. Cognee `ContextProjection` adapter + receipt/idempotency manifest;
3. backend contract matrix и shadow activation prototype;
4. Context Broker authorization/evidence revalidation tests;
5. Temporal-owned cancellation/recovery contract для projection Activities.

Эти tickets являются prerequisites R1 Context Broker. Они не требуют новой
runtime dependency, пока backend prototype не выберет production graph store.
