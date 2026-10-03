# Нужен ли Cognee платформе Spine

Исследование выполнено 13 сентября 2026 года по официальной документации и
репозиториям рассматриваемых проектов. Это рекомендация по выбору реализации
Context Broker для первых четырёх кейсов, а не решение о немедленной миграции.

## Обязательное архитектурное требование

Spine должен иметь не только vector retrieval, но и Context Graph: версионируемую
проекцию ontology, domain entities и evidence-linked relations. Граф нужен для
описания предметной области, multi-hop retrieval, provenance traversal и связи
объектов из разных источников. PostgreSQL остаётся источником истины, а графовая
БД — перестраиваемой semantic projection.

## Решение в одном абзаце

PostgreSQL/pgvector в одиночку не является заменой Cognee. После фиксации
обязательного Context Graph разумны два варианта:

1. Cognee как graph/vector projection за Context Broker;
2. owned stack: graph database + управляемый Spine ontology/extraction pipeline
   + vector/full-text retrieval.

Cognee остаётся ведущим кандидатом, потому что уже объединяет извлечение
entities/relations, ontology, graph и retrieval. Заменять его имеет смысл только
на явно спроектированный graph stack, если Cognee мешает stable provenance, ACL,
versioning или эксплуатации. LlamaIndex и Haystack сами по себе такой заменой не
являются. Bake-off должен сравнивать Cognee с owned graph stack, а не с одним
pgvector.

## Что именно заменяется

Замена касается только реализации двух операций за Context Broker seam:

```text
project(SourceRevision, ProjectionVersion) -> ProjectionReceipt
retrieve(ContextQuery, AccessContext) -> RetrievalResult
```

Не заменяются canonical Postgres models, `SourceRevision`, Artifact Store,
Temporal, Agent Runtime, evaluation и UI. Поэтому решение обратимо, если API,
workflow и agents не вызывают `cognee.*` напрямую.

## Кандидаты

| Кандидат | Что это | Сильная сторона для Spine | Главный недостаток |
|---|---|---|---|
| PostgreSQL + pgvector | Vector/full-text lane и metadata index, но не полный Context Graph | ACL, revisions, locators и vectors находятся рядом с canonical metadata | Не выполняет обязательное требование ontology/entity/relation graph самостоятельно |
| Cognee | Semantic-memory framework с graph/vector/relational stores и pipelines | Быстрый путь к graph, ontology, temporal и hybrid experiments | Собственная lifecycle/data model; citations и ACL всё равно приходится оборачивать Spine |
| Owned graph stack | Graph store + собственная projection pipeline + vector lane | Полный контроль ontology, provenance, tenancy, revisions и retrieval contract | Существенно больше extraction/rebuild кода принадлежит Spine |
| LlamaIndex | Framework «interface between LLMs and data» с loaders, nodes, indexes и retrievers | Богатый набор retrieval building blocks и integrations | Не является самостоятельной production data/authorization architecture; возможности зависят от выбранного store |
| Haystack | Framework типизированных indexing/query/agent pipelines | Явные components, async pipelines, filters и document-store adapters | Его pipeline может конкурировать с Temporal; не решает canonical revisions, lineage и ACL автоматически |
| Microsoft GraphRAG | Специализированный graph indexing/query engine | Сильный global/local corpus reasoning | Дорогая LLM-indexing pipeline, отдельные Parquet/vector outputs и migrations; избыточен для R1 |
| Graphiti | Temporal knowledge-graph framework | Интересен для изменяющихся facts и событий R3/R4 | Не заменяет document retrieval R1; требует отдельного graph store и собственной интеграции provenance/ACL |

## Роль PostgreSQL + pgvector

Spine уже выбрал PostgreSQL как canonical store. pgvector хранит embeddings рядом
с обычными колонками и даёт exact/approximate nearest-neighbour search, Postgres
full-text search и hybrid composition через RRF или reranker. Это позволяет
применять `workspace_id`, security domain, source revision, tombstone и object ACL
в одном запросе и одной транзакционной модели. Это сильный vector/full-text lane
и контрольный retrieval baseline, но не замена графовой проекции.

[Официальный pgvector README](https://github.com/pgvector/pgvector/blob/master/README.md)
описывает exact search, HNSW/IVFFlat, фильтрацию обычным SQL, hybrid search с
Postgres FTS, re-ranking, backup/replication и мониторинг. При этом filtered ANN
нельзя считать корректным автоматически: документация предупреждает, что filter
применяется после approximate scan и рекомендует iterative scans. Для Spine
нужны benchmark разных ACL-selectivity и сравнение ANN с exact ground truth.

Минимальная схема projection может хранить:

```text
retrieval_chunks
  workspace_id
  security_domain_id
  source_revision_id
  locator
  content
  content_tsvector
  embedding
  embedding_model_version
  projection_version
  valid_from / valid_to
```

Стабильная citation строится непосредственно из `source_revision_id + locator`,
а не восстанавливается из текста ответа. RLS остаётся дополнительной защитой к
проверке policy в Context Broker.

Для parsing эта альтернатива не обязана писать всё самостоятельно. Например,
[Docling](https://github.com/docling-project/docling) поддерживает PDF, DOCX,
XLSX, Markdown и другие форматы, structured JSON и chunking; его можно поставить
за отдельный parser adapter. Parser не должен определять retrieval architecture.

## Почему Cognee теперь является default-кандидатом

Обязательный Context Graph означает, что следующие свойства нужны архитектурно,
а не только как возможная оптимизация:

- entity/relation extraction по нескольким документам;
- graph traversal для multi-hop вопросов;
- ontology-constrained projection;
- temporal relations;
- hybrid graph/vector retrieval, превосходящего chunk baseline;
- быстрого эксперимента с новыми projection strategies.

Cognee имеет Apache-2.0 лицензию и поддерживает Python 3.10–3.14 согласно
[официальному `pyproject.toml`](https://github.com/topoteretes/cognee/blob/main/pyproject.toml),
поэтому лицензия и текущий диапазон Python сами по себе не являются причиной
замены.

Но [разбор Cognee examples](cognee-examples-platform-fit.md) показал, что Spine
всё равно должен самостоятельно обеспечить stable citations, object ACL,
canonical revisions, typed retrieval, controlled deletion/rebuild и evaluations.
Если Cognee скрывает существенную сложность ontology extraction, graph projection,
hybrid retrieval и rebuild за небольшим adapter interface, module остаётся
глубоким. Он становится плохим выбором только если Spine вынужден обходить его
модель или повторно реализовать эти свойства вокруг него.

## Почему LlamaIndex и Haystack не являются прямой заменой

### LlamaIndex

LlamaIndex предоставляет loaders, ingestion transformations, nodes, retrievers,
metadata filters и citation query engine. Его
[CitationQueryEngine](https://docs.llamaindex.ai/en/stable/api_reference/query_engine/citation/)
упрощает prompt-level citations, но Spine всё равно должен связывать каждый node
с собственной ревизией и locator и детерминированно проверять ссылки.

Поддержка metadata filters, delete и async различается между vector-store
integrations; это видно даже из официальной
[таблицы vector stores](https://docs.llamaindex.ai/en/stable/module_guides/storing/vector_stores/).
Следовательно, LlamaIndex может уменьшить код retriever composition, но не
устраняет выбор и тестирование фактического backend. Кроме того, его собственные
workflow/query abstractions не должны становиться вторым durable orchestrator.

### Haystack

Haystack предоставляет typed pipeline connections, sync/async pipelines,
retrievers и document-store integrations. Его
[metadata filtering](https://docs.haystack.deepset.ai/docs/metadata-filtering)
полезен, но поддерживаемые operators зависят от конкретного Document Store.
[AsyncPipeline](https://docs.haystack.deepset.ai/reference/pipeline-api) управляет
графом components и streaming partial outputs, однако durability, approvals и
business retries всё равно принадлежат Temporal.

Haystack хорош как внутренняя библиотека для bounded indexing/query pipeline,
если она заметно сокращает реализацию. Делать его платформенным seam вместо
Context Broker не следует.

Оба проекта имеют permissive лицензии: LlamaIndex — MIT, Haystack — Apache-2.0
([LlamaIndex project metadata](https://github.com/run-llama/llama_index/blob/main/pyproject.toml),
[Haystack license](https://github.com/deepset-ai/haystack/blob/main/LICENSE)).

## Альтернативные graph implementations

[Microsoft GraphRAG](https://microsoft.github.io/graphrag/) извлекает entities,
relations и claims, строит communities и summaries, затем предоставляет global,
local, DRIFT и basic search. Это полезно для вопросов, требующих целостного обзора
большого корпуса. Но официальный indexer создаёт собственные Parquet outputs и
vector indexes, требует LLM-heavy indexing и migration/config management. Для
операционной предметной модели Spine он ориентирован прежде всего на corpus
reasoning и community summaries. Его стоит benchmark как retrieval strategy, а
не принимать за весь Context Graph.

[Graphiti](https://github.com/getzep/graphiti) специально строит temporal
knowledge graphs для постоянно меняющейся agent memory. Он интереснее для R3/R4,
где важны последовательность коммуникаций и изменение фактов, но не заменяет
парсинг и chunk retrieval документов. Его можно позднее сравнить с Cognee как
graph projection, а не с PostgreSQL baseline целиком.

Прямой owned вариант — graph store вроде Neo4j, собственные versioned ontology
schemas и extraction/projection tasks Spine. Современный Neo4j поддерживает
[full-text и vector semantic indexes](https://neo4j.com/docs/cypher-manual/25/indexes/semantic-indexes/),
поэтому возможна как единая graph/vector projection, так и комбинация Neo4j с
pgvector. Конкретную топологию следует выбирать нагрузочными, ACL и rebuild
тестами.

Текущая архитектура называет Kuzu local backend, но официальный
[репозиторий Kuzu](https://github.com/kuzudb/kuzu) архивирован 10 октября 2025
года. Для нового production-oriented проекта это риск сопровождения. Local graph
backend нужно пересмотреть отдельно; archived Kuzu нельзя оставлять
непроверенным default только потому, что его использует Cognee example.

## Fit по продуктовым релизам

| Релиз | Обязательная graph/vector projection | Развитие ontology |
|---|---|---|
| R1 Q&A | Context Graph + chunk/vector/full-text lanes + stable citations и ACL | Ontology, entities и relations строятся сразу; eval определяет их вклад в retrieval |
| R2 КП | Граф требований, клиентов, аналогов и pricing provenance | Версионированные коммерческие типы и связи |
| R3 Коммуникации | Temporal communication graph поверх canonical messages/revisions | Participants, questions, commitments и identity candidates |
| R4 Задачи | Task/responsibility/dependency graph поверх canonical timeline | Граф объясняет блокировки, но статусы и SLA остаются в Postgres |

## Bake-off вместо миграции по вере

### Общий контракт

Cognee и owned graph implementation должны возвращать один `RetrievalResult`:
chunks, entities, relations, references, scores, strategy, projection version и
degradation flags. Ни один evaluator не должен знать, какой adapter использовался.

### Dataset

Нужен versioned R1 golden dataset минимум с такими strata:

- точный факт на одной странице;
- таблица XLSX и таблица PDF;
- multi-chunk synthesis;
- multi-document и multi-hop вопрос;
- устаревшая и новая ревизия одного документа;
- удалённый/tombstoned документ;
- разрешённый и запрещённый объект;
- отсутствие достаточного evidence;
- русский и смешанный русский/английский текст.

### Метрики и gates

- retrieval recall@k и MRR по reference IDs;
- citation precision/recall и locator correctness;
- ACL leakage — строго 0;
- stale/deleted evidence — строго 0;
- grounded answer/abstention quality;
- p50/p95 latency и indexing lag;
- стоимость embeddings и LLM extraction;
- update/delete/rebuild correctness;
- операционное время на backup, restore, upgrade и diagnosis.

### Правило выбора

Оставить Cognee, если его adapter обеспечивает ontology/entity/relation
projection, stable evidence mapping, workspace isolation, update/delete/rebuild и
приемлемое качество retrieval без обхода публичных interfaces.

Выбрать owned graph stack, если Cognee не проходит обязательные provenance, ACL
или rebuild gates либо его внутреннюю модель приходится фактически дублировать в
Spine. Качество chunk/vector lane сравнивается отдельно, но успешный pgvector
baseline не отменяет обязательный Context Graph.

## Рекомендация для текущего проекта

1. Не удалять Cognee из прототипа до появления второго работающего adapter.
2. Сначала стабилизировать `ContextQuery`, `RetrievalResult`, citation locator и
   projection commands — это настоящая interface seam.
3. Довести Cognee adapter до минимального graph-capable vertical slice: ontology,
   entities, relations, provenance и hybrid retrieval.
4. Реализовать PostgreSQL/pgvector lane как контроль качества chunk/vector
   retrieval, а не как полноценную альтернативу Context Graph.
5. После выбора сохранить проигравший adapter только если его поддержка дёшева;
   не строить постоянную dual-write архитектуру без продуктовой причины.
6. Прототипировать owned graph adapter только если Cognee не проходит обязательные
   contract tests; не строить две постоянные graph projections.

Итого: замена Cognee технически пока недорога, потому что текущая интеграция
сосредоточена в compatibility helper, а целевой adapter ещё не реализован. Но
заменой может быть только другой graph-capable stack. Текущее рабочее решение —
продолжить с Cognee за Context Broker, одновременно заранее проверяя условия, при
которых понадобится owned ontology/extraction/graph implementation.
