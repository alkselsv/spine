# Архитектуры Company Brain и Spine

Исследование выполнено 13 сентября 2026 года по интерактивной
[главе 5 Company Brain от Slite](https://slite.com/ebooks/company-brain/content/chapter-5#chapter-5-inside-real-company-brains)
и текущим документам и коду Spine. Это архитектурное сравнение, а не предложение
заменить roadmap или немедленно выбрать новый memory backend.

## Вывод

Spine не соответствует одной конкретной архитектуре из каталога Slite. Его
целевая модель объединяет наиболее сильные свойства нескольких подходов:

- каноническое операционное состояние и неизменяемые ревизии источников;
- перестраиваемую graph/vector-проекцию, близкую по роли к GBrain, Cortex и
  Zep/Graphiti;
- human-reviewed изменения, как у Sylph, Carrara и Slite Agent;
- выдачу контекста людям и агентам через единый контролируемый слой;
- поверх Company Brain — типизированные agent/code/human workflows, evaluations,
  approvals и управляемые внешние действия.

На уровне целевой архитектуры это более широкий и более строгий контур, чем
любая отдельная схема из главы. На уровне работающего продукта картина обратная:
Spine пока является локальным Q&A-прототипом с прямыми `cognee.remember()` и
`cognee.recall()`. Коннекторы, immutable source revisions, ACL, управляемое
обслуживание памяти, Context Broker, UI и durable workflows ещё находятся в
roadmap или представлены каркасом
([README](../../README.md), [roadmap](../ROADMAP.md),
[текущий API](../../src/spine/api/main.py),
[Cognee helper](../../src/spine/memory/cognee_memory.py)).

Практический вывод: Spine не нужно копировать одну из десяти реализаций. Полезнее
принять четыре компонента Slite как проверку полноты каждого вертикального
релиза, а ближайший пробел сформулировать как явный **maintenance loop**:
обнаружение устаревших, конфликтующих и неподтверждённых знаний → предложение
изменения → evaluation/policy → human approval при необходимости → новая версия
проекции → regression check и rollback.

## Как Slite раскладывает Company Brain

Slite утверждает, что разные реализации сводятся к четырём компонентам
([обзор модели](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-every-company-brain-has-the-same-four-components)):

1. **Getting signals** — поступление событий и контекста из рабочих систем.
2. **Remembering** — долговременное представление памяти.
3. **Dreaming and pruning** — консолидация, поиск пробелов и противоречий,
   устаревание и очистка.
4. **Speaking and searching** — выдача контекста людям и агентам.

Глава 5 показывает эти четыре «порта» на реальных реализациях
([интерактивная схема](https://slite.com/ebooks/company-brain/content/chapter-5#chapter-5-inside-real-company-brains-1-vendor-chips)).
В метаданных визуала по-прежнему написано «Nine company brains, four ports», но
его текущий набор содержит десять записей: к девяти схемам добавлена Carrara.

## Десять архитектур из главы

Все технические описания в таблице — пересказ интерактивной схемы Slite;
«компромисс» — вывод из показанного механизма, а не цитата авторов.

| Архитектура | Signals → remembering → dreaming/pruning → speaking | Основной компромисс |
|---|---|---|
| **GBrain** | Плановые сборщики email/calendar, webhooks, voice notes, inbox и skillpacks → Markdown-репозиторий как system of record, PGLite или Postgres/pgvector, wikilinks как типизированные рёбра → ночной dream cycle чинит ссылки и цитаты, консолидирует память и отмечает stale/uncited/contradictory context → hybrid vector/keyword search с rank fusion и цитируемым ответом | Прозрачность, переносимость и Git-аудит ценой самостоятельного владения коллекторами, расписанием, качеством графа и эксплуатацией. |
| **mem0** | Приложение явно вызывает `add()` для выбранных разговоров → LLM извлекает факты в vector memory со scope user/agent/session/organization; graph memory доступна в hosted-варианте → история сохраняется, а query-time decay повышает свежие и часто используемые записи → semantic, keyword и entity search через SDK/MCP | Удобная память приложения или агента, но не готовая общеорганизационная knowledge architecture; полнота входа зависит от caller, качество — от LLM extraction. |
| **Letta** | Агент записывает память tool calls, также доступны API и integrations → небольшие context blocks, поисковый архив и полная message history в Postgres → sleep-time agent переписывает shared memory, compaction суммирует часть сообщений при заполнении контекста → агент использует memory tools, люди проверяют результат в ADE, разработчики обращаются через REST/SDK | Сильная agent-centric memory, но агент получает значительную власть над тем, что считается памятью; rewrite и compaction требуют контроля качества. |
| **Zep / Graphiti** | Conversations, JSON и документы приходят как episodes → temporal graph из raw events, entities/relations и communities с embeddings и временными метками → противоречащий новый факт завершает срок действия старой связи, а не удаляет её; fuzzy matching обрабатывает вероятные дубли → semantic + keyword + graph traversal без LLM на retrieval path | Лучше остальных моделирует изменение истины во времени, но требует сложных graph, extraction и identity-resolution контуров. |
| **Sylph** | Команда сама настраивает MCP-коннекторы, prompts могут читать live context → Git tree с корневым `CONTEXT.md`, доменными папками и Markdown → агенты кладут изменения в `_drafts/`, человек принимает результат, система учится на diff → slash-команды в Claude Code, Codex или Cursor; проверка через `git diff` и `grep` | Максимальная наблюдаемость и человеческий контроль, но интеграции, структура и review-flow принадлежат команде. |
| **DIY: Claude Code + Git** | Люди обновляют знания через PR, subagents возвращают компактные summaries → `CLAUDE.md`, rules, skills, runbooks и Markdown в Git → session compaction вручную, долговременная очистка через human review → read/glob/grep без semantic index | Самый простой и дешёвый инженерный вариант; плохо масштабируется на большой объём, неоднородные источники, permissions и автоматическую freshness. |
| **Pletor** | Brand nodes и data feeds принимают guidelines, references, performance и competitor signals в text/image/video/audio → living Brand Brain связывает заявления, материалы и результаты → люди сохраняют решения о taste/performance, feedback питает следующий creative cycle → chat, voice agents и canvas workflows | Глубокая вертикальная модель маркетинга вместо универсального Company Brain; качество опирается на человеческое суждение. |
| **Gorgias Cortex** | Airbyte и собственный ingestion читают Postgres, Notion, Gong, GitHub, Linear и HubSpot; transcripts chunked и embedded → около 12 000 типизированных Markdown-узлов в GitHub образуют knowledge graph → traces неудачных разговоров выявляют пробелы, scheduled loop группирует исправления в PR → агент начинает с task-specific skills и обходит связи до достаточного контекста | Сильный feedback-to-maintenance loop и проверяемые PR, но высокая стоимость собственной схемы, ingestion и постоянной команды сопровождения. |
| **Carrara** | Почасовой импорт Granola, Gmail, Calendar и Slack с identity resolution; дополнительные источники через Composio → shared memory в Neon Postgres и локальная реплика в macOS app → каждое воспоминание проходит acceptance UI, автоматического pruning пока нет → custom MCP для агентов и native app для людей | Approval-first повышает доверие, но образует ручную очередь; импорт не real-time, maintenance неполон, human surface привязан к macOS. |
| **Slite Agent** | Live sync с 20+ источниками → документы knowledge base имеют verification tags, owners и expiry windows → auto-maintenance сверяет документы с корпоративными данными и показывает side-by-side diff для человеческого решения → цитируемые ответы в UI/API и доступ агентов через MCP | Managed human-governed вариант уменьшает инфраструктурную нагрузку, но остаётся document-centric и создаёт зависимость от поставщика и набора его коннекторов. |

## Сопоставление четырёх компонентов со Spine

Важно различать [целевую архитектуру Spine](../ARCHITECTURE.md) и фактически
работающий код. Большинство преимуществ Spine ниже пока являются архитектурными
контрактами или планом, а не проверенными production-возможностями.

| Компонент Slite | Целевая модель Spine | Что есть сейчас | Разрыв |
|---|---|---|---|
| **Getting signals** | Connector workers, webhook/incremental sync, immutable raw payload, `SourceObject`/`SourceRevision`, нормализация, identity resolution и transactional outbox | Локальная загрузка `.md`, `.txt`, `.pdf`, `.docx`, `.xlsx`; базовый connector protocol | Нет рабочих внешних коннекторов, cursors, immutable originals, revisions, identity resolution, outbox и production idempotency ([loader](../../src/spine/ingest/loaders.py), [connector contract](../../src/spine/connectors/base.py)). |
| **Remembering** | PostgreSQL как источник истины Spine; оригиналы в object storage; Cognee как versioned, rebuildable graph/vector Context projection; facts отделены от model conclusions | Тексты напрямую передаются в Cognee dataset; есть только provider-neutral retrieval contract и доменный каркас | Каноническое хранилище, projection lifecycle, stable locators, ontology versions и receipts не реализованы ([ADR 0001](../adr/0001-canonical-store-and-context-graph.md), [memory contract](../../src/spine/memory/contracts.py)). |
| **Dreaming and pruning** | `ProjectionVersion`, shadow rebuild, eval/ACL leakage gates, ontology candidates, tombstones, rollback; вывод становится фактом только после validation/human decision | Явного maintenance workflow нет; API лишь добавляет данные и вызывает recall | Это наименее реализованный порт. Нет stale/contradiction/coverage loop, очереди поправок, controlled deletion/rebuild и feedback-driven maintenance. |
| **Speaking and searching** | Context Broker применяет tenant/role/purpose policy и возвращает typed chunks/entities/relations/references, strategy и index version; сохраняется выданный `ContextBundle`; UI показывает evidence и freshness | `/ask` напрямую вызывает Cognee, сворачивает результат в plain-text answer и принимает dataset от caller | Нет object-level ACL, проверяемых citations, abstention, context-profile enforcement, сохранённого context bundle и пользовательского citation inspector ([API](../../src/spine/api/main.py), [roadmap R1](../ROADMAP.md#r1--qa-по-внутренним-документам)). |

## Где Spine совпадает с конкретными подходами

### Наиболее близкие родственники

- **GBrain** близок разделением durable record и поискового слоя, графовыми
  связями, cited hybrid retrieval и явным периодическим обслуживанием. Spine
  выбирает более строгий operational record: PostgreSQL и immutable source
  revisions вместо Markdown как общей истины.
- **Zep/Graphiti** наиболее релевантен будущим R3/R4, где сообщения, обязательства,
  статусы и блокировки меняются во времени. Его temporal graph может быть
  кандидатом на projection adapter, но не должен заменять канонические ревизии и
  факты внешних систем.
- **Gorgias Cortex** ближе всего к масштабу задуманного owned Company Brain:
  много источников, типизированный graph, task-specific retrieval и loop от
  неудачного ответа к проверяемому улучшению. Spine дополнительно формализует
  tenancy, immutable definitions, typed artifacts, handoff gates и действия.
- **Carrara и Slite Agent** дают полезный образец продуктовой поверхности для
  proposed memory changes. В целевой модели Spine есть policy/eval/human gates,
  но отдельная acceptance/maintenance inbox ещё не описана как законченный
  пользовательский workflow.

### Частичный fit

- **mem0 и Letta** решают память отдельного приложения или агента. Они могут быть
  внутренней реализацией ограниченного agent memory, но противоречат Spine, если
  агентская память становится неаудируемой компанией истиной или вторым durable
  workflow store. В Spine агент stateless между вызовами, а долгоживущее
  состояние принадлежит workflow и canonical stores
  ([ADR 0010](../adr/0010-capability-based-agent-runtime.md)).
- **Sylph и DIY Git** хорошо подходят для versioned definitions, prompts, skills,
  runbooks и ADR — именно так уже организована часть самого репозитория Spine.
  Они не покрывают operational objects, heterogeneous ingestion, row/object ACL,
  high-volume event history и idempotent actions.
- **Pletor** подтверждает принцип Spine «vertical slice first»: полезная ontology,
  интерфейс и feedback возникают из конкретного бизнес-кейса. Но brand/creative
  ontology не отвечает четырём приоритетным кейсам Spine и не должна становиться
  общей моделью платформы.
- **Slite Agent** может быть источником документов или внешним context provider,
  но не заменяет Spine целиком: Spine должен также владеть процессами,
  типизированными артефактами, approvals, audit и внешними действиями.

## В чём целевая модель Spine сильнее каталога

1. **Разделение истин.** Внешняя система остаётся авторитетной для своего объекта;
   PostgreSQL хранит состояние Spine; Context Graph — перестраиваемая проекция.
   Ни retrieval result, ни агентская память не перезаписывают факт автоматически.
2. **Provenance как инвариант.** Существенный вывод ссылается на конкретную
   `SourceRevision`, locator и upstream artifacts, а не только показывает
   человекочитаемую citation.
3. **Безопасность на выдаче и действии.** Планируются workspace/environment,
   object-level access, purpose-aware retrieval, scoped tool grants, policy и
   approval. В схемах Slite permissions описаны непоследовательно.
4. **Версионируемое исполнение.** Capability, agent, prompt, context profile,
   workflow, evaluator и artifact schema закрепляются на run; долгие retries и
   ожидания человека принадлежат durable workflow layer.
5. **Проверка бизнес-результата.** Evaluation охватывает retrieval, handoff и
   detector quality, а value агрегируется до workflow outcome, а не заканчивается
   на «ответ найден».

Эти свойства заданы в архитектуре, но ещё не доказаны реализацией. Сейчас тесты
проверяют в основном чистоту domain boundary и несколько workflow-инвариантов
([architecture scaffold tests](../../tests/test_architecture_scaffold.py)).

## Что Spine следует перенять

### 1. Сделать maintenance loop видимой частью Context plane

Текущая архитектура хорошо описывает projection build/rebuild, но слабее отвечает
на ежедневный вопрос: кто обнаруживает knowledge gap, contradiction или stale
source и кто принимает поправку. Для R1 достаточно минимального контура:

```text
retrieval/feedback/sync signal
  → MaintenanceFinding
  → proposed change or reindex command
  → deterministic + retrieval evals
  → human review when policy requires
  → new immutable version
  → shadow validation
  → activate or rollback
```

GBrain даёт scheduled health pass, Gorgias — feedback from failed traces, Slite —
owners/expiry/diff triage, Sylph и Carrara — запрет self-promotion без человека.
Spine может объединить эти механизмы, не позволяя LLM напрямую изменять
канонические факты.

### 2. Добавить maintenance/acceptance inbox в соответствующий UI-срез

Для R1 citation inspector и feedback уже обязательны. Логичное минимальное
расширение — очередь stale/conflict/uncited findings и предложенных изменений с
evidence, diff, owner, причиной, version и решением. Это не универсальный workflow
builder и не требует двигать R5 вперёд.

### 3. Проверять каждый релиз по четырём портам

Четыре компонента Slite полезны как короткий архитектурный чек-лист:

| Релиз | Signals | Remembering | Dreaming/pruning | Speaking/searching |
|---|---|---|---|---|
| **R1 Q&A** | Файлы и revisions | Document/context graph | freshness, contradiction, reindex, feedback | cited ACL-safe Q&A и inspector |
| **R2 КП** | Реестр, запросы, материалы | Requirements, analogues, pricing provenance | outdated price/template detection, human corrections | structured editor, diff и evidence |
| **R3 Коммуникации** | Incremental message events | Temporal conversation graph | dedup, reopen, suppression, replay | Findings Inbox, timeline, digest |
| **R4 Задачи** | Tasks/comments/status changes | Canonical task timeline и relations | late-event recomputation, SLA changes, feedback | blocker/responsibility view и controlled escalation |

Это усиливает уже зафиксированную поставку вертикальными срезами
([roadmap](../ROADMAP.md)) и не превращает Company Brain в отдельный
горизонтальный мегапроект.

### 4. Сохранить provider-neutral seam

Ни GBrain, ни Graphiti, ни mem0, ни Slite не следует делать новым системным
контрактом. Их разумно оценивать как adapter или внешний source по единому
`ContextProjection`/`ContextBroker` contract: stable references, ACL, index
version, degradation flags, rebuild и eval. Это сохраняет возможность сочетать
temporal graph Graphiti, document source Slite или другой backend без изменения
capability/workflow contracts.

### 5. Не копировать Git как operational source of truth

Git/Markdown остаётся хорошим носителем ADR, prompts, skills, policies и других
reviewable definitions. Для сообщений, задач, approvals, work items, external
actions и tenant-owned state выбранный Spine PostgreSQL лучше соответствует
concurrency, query, ACL, idempotency и event-history требованиям. PR можно
использовать как review surface для repository-owned knowledge, но не как общий
runtime protocol.

## Приоритеты относительно roadmap

Исследование не даёт причины менять порядок R1 → R2 → R3 → R4. Наоборот,
разнообразие десяти схем подтверждает риск преждевременного универсального
Company Brain. Рекомендуемая последовательность:

1. Закрыть R1 end-to-end: revisions, locators, ACL, Context Broker, cited API/UI,
   evals и минимальный maintenance loop.
2. Проверить тот же контекстный контракт на R2, где особенно важны human diff,
   pricing provenance и запрет LLM-арифметики
   ([кейс КП](<../cases/ИИ-ассистент для генерации коммерческих предложений.md>)).
3. Только в R3/R4 benchmark temporal graph against current Cognee projection на
   реальных timeline/replay задачах
   ([кейс коммуникаций](<../cases/ИИ-ассистент для мониторинга клиентских коммуникаций.md>),
   [кейс задач](<../cases/ИИ-ассистент для контроля исполнения внутренних задач.md>)).
4. Выносить общий maintenance product и backend choice в платформенный уровень
   лишь после подтверждения минимум двумя вертикальными кейсами.

## Ограничения исследования

- Глава Slite — интерактивный динамический материал без отдельных стабильных
  URL для каждой карточки; все десять описаний ссылаются на общий anchor visual.
- Число архитектур на странице сейчас расходится с подписью самого визуала:
  фактически 10 против заявленных 9.
- Для закрытых систем Slite, Carrara, Pletor и Gorgias сравнение опирается на
  описание, опубликованное Slite; внутреннюю реализацию независимо проверить
  невозможно.
- Целевая архитектура Spine меняется в текущем worktree. Сравнение намеренно
  отделяет принятые документы/roadmap от минимального работающего прототипа и не
  трактует scaffold directories как готовые capabilities.

## Источники

- Slite: [Visualising real architectures from the field](https://slite.com/ebooks/company-brain/content/chapter-5#chapter-5-inside-real-company-brains)
  и [интерактивная схема архитектур](https://slite.com/ebooks/company-brain/content/chapter-5#chapter-5-inside-real-company-brains-1-vendor-chips).
- Slite: [четыре компонента Company Brain](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-every-company-brain-has-the-same-four-components),
  включая [signals](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-getting-signals),
  [remembering](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-remembering),
  [dreaming/pruning](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-dreaming-and-pruning)
  и [speaking/searching](https://slite.com/ebooks/company-brain/content/chapter-4#chapter-4-speaking-and-searching).
- Spine: [ARCHITECTURE.md](../ARCHITECTURE.md),
  [ROADMAP.md](../ROADMAP.md), [README.md](../../README.md),
  [ADR 0001](../adr/0001-canonical-store-and-context-graph.md) и
  [ADR 0010](../adr/0010-capability-based-agent-runtime.md).
- Spine implementation: [legacy Q&A API](../../src/spine/api/main.py),
  [Cognee helper](../../src/spine/memory/cognee_memory.py),
  [ingestion loaders](../../src/spine/ingest/loaders.py),
  [Context Broker contract](../../src/spine/memory/contracts.py) и
  [architecture tests](../../tests/test_architecture_scaffold.py).
