# Как реализованы агенты в Agno

Исследование выполнено 13 сентября 2026 года по текущей официальной
документации Agno и ветке `main` репозитория `agno-agi/agno`. Его цель —
уточнить, какие формы подключаемых агентов должен учитывать независимый от
фреймворка контракт Spine. Agno не предлагается добавлять в зависимости Spine.

## Краткий вывод

В Agno основной встроенный агент — это конфигурируемый Python-объект класса
`Agent`. Пользователь обычно не наследуется от него, а собирает экземпляр из
модели, инструкций, tools, knowledge, memory/database и hooks. Однако граница
AgentOS уже построена не на проверке конкретного класса: она принимает любой
объект, структурно удовлетворяющий небольшому `AgentProtocol`. Для удалённых
агентов есть `RemoteAgent`, работающий через AgentOS REST или A2A.

Следовательно, принятое для Spine направление — небольшой `Protocol` плюс
адаптер async-функций — совпадает с сильной стороной архитектуры Agno. Переносить
в этот протокол весь конструктор `Agent`, состояние сессии, детали модели,
knowledge и streaming-union не следует.

## Встроенный `Agent`

`agno.agent.Agent` — `@dataclass(init=False)` с собственным большим
конструктором. В объекте конфигурируются модель и fallback-модели, инструкции,
tools, knowledge/RAG, Pydantic-схемы ввода и вывода, hooks, retries, database,
история, пользовательская память, session state, streaming и telemetry. Сам
класс делегирует исполнение специализированным внутренним модулям, например
`_run`, `_session`, `_tools` и `_messages`.[Исходный код `Agent`](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/agent/agent.py)

Типичный агент создаётся композицией, а не отдельным подклассом:

```python
agent = Agent(
    id="document-qa",
    model=model,
    instructions="Answer only from accessible evidence.",
    tools=[search_documents],
    input_schema=Question,
    output_schema=Answer,
    db=db,
)
```

Официальные примеры последовательно предлагают создавать `Agent(...)`,
передавая зависимости и поведение через параметры. Для повторно используемых
наборов инструментов наследование применяется к `Toolkit`, а не к самому
агенту.[Agent tools](https://docs.agno.com/tools/agent)
[Custom toolkits](https://docs.agno.com/tools/creating-tools/toolkits)

## Минимальный интерфейс AgentOS

В исходном коде есть отдельный `@runtime_checkable AgentProtocol`. Его должны
удовлетворять native `Agent`, `RemoteAgent` и адаптеры других фреймворков. В
контракт входят только:

- свойства `id` и `name`;
- метод `arun(input, *, stream, session_id, user_id, media,
  stream_events, **kwargs)`;
- результат `RunOutput` либо асинхронный iterator событий при streaming.

[Исходный код `AgentProtocol`](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/agent/protocol.py)

Это structural typing: внешний adapter не обязан наследоваться от `Agent`.
Показательно, что даже Agno отделяет богатую native-реализацию от минимальной
границы регистрации в runtime.

## Запуск и lifecycle

У встроенного агента есть синхронный `run()` и асинхронный `arun()`. Перед
обращением к модели runtime собирает system/user messages, историю, memories и
session state; затем выполняет цикл «ответ модели — tool call — результат tool»
до финального ответа. Без streaming возвращается `RunOutput`, со streaming —
события, включая tool, memory, reasoning, pause, cancellation и terminal
events.[Running Agents](https://docs.agno.com/agents/running-agents)

API также поддерживает продолжение приостановленного run, отмену и background
execution. Обычный SDK background-run живёт только в текущем процессе; для
восстановления после рестарта AgentOS требует durable queue. Это важное отличие
между интерфейсом агента и долговечной оркестрацией.[Running Agents — background execution](https://docs.agno.com/agents/running-agents#background-execution)

## Tools и контролируемые действия

Tool может быть обычной Python-функцией, объектом `Function`, `Toolkit` или
фабрикой набора tools, вычисляемой во время run. `Toolkit` группирует функции и
задаёт фильтрацию, cache, timeout и режимы исполнения. Hooks позволяют обернуть
tool call проверками и наблюдаемостью.[What are Tools?](https://docs.agno.com/tools/overview)
[Toolkit reference](https://docs.agno.com/reference/tools/toolkit)

Чувствительный tool можно пометить `requires_confirmation=True`: run
останавливается, возвращает незавершённые requirements и продолжается после
явного решения пользователя через `continue_run()`.[User Confirmation](https://docs.agno.com/hitl/user-confirmation)

Для Spine из этого полезны реестр tools, run context, события и явный pause /
resume. Но права, идемпотентность и approval должны обеспечиваться платформой
Spine, а не только декларацией в коде агента.

## Sessions, state и memory

Agno разделяет историю разговора в пределах `session_id` и долговременную память
пользователя в пределах `user_id`. Рабочий `session_state` находится в run
context и сохраняется в database; значение на `Agent` служит default для новых
сессий. Если `session_id` не передан, первый run создаёт его и сохраняет на
экземпляре `Agent`, а следующие вызовы этого экземпляра повторно используют
идентификатор.[Agent Session State](https://docs.agno.com/state/agent/overview)
[Sessions and memory](https://docs.agno.com/use-cases/product-agents/sessions-and-memory)

Spine не должен повторять смешение конфигурации реализации и default session
identity. `AgentVersion` должен быть неизменяемой конфигурацией, а
`workspace_id`, `environment`, `run_id`, субъект доступа и ссылки на состояние
должны приходить в каждый `AgentInvocation` / `AgentExecutionContext`.

## Типизированный ввод и вывод

`Agent` принимает строку, список, dictionary, `Message`, Pydantic model или
список сообщений. `input_schema` валидирует ввод, а `output_schema` может быть
Pydantic-моделью или JSON Schema. При наличии native structured output Agno
передаёт схему модели, иначе запрашивает JSON и разбирает его самостоятельно.
[Agent reference](https://docs.agno.com/reference/agents/agent)

Даже при заданной схеме ошибка fallback parsing может оставить
`response.content` строкой; документация требует проверять фактический тип.
[Structured Output for Agents](https://docs.agno.com/input-output/structured-output/agent)

Поэтому для Spine типизированные artifacts должны валидироваться runtime-слоем
как обязательный контракт capability. Невалидный результат должен стать явным
ошибочным исходом run, а не тихо превратиться в свободный текст.

## Teams и workflows

Agno разделяет две формы композиции:

- `Team` объединяет agents и вложенные teams; leader динамически координирует,
  маршрутизирует, рассылает одну задачу всем либо ведёт общий task loop;
- `Workflow` задаёт повторяемый control flow из agents, teams, функций и
  вложенных workflows с последовательностями, parallel, conditions, loops и
  routers.

[Team reference](https://docs.agno.com/reference/teams/team)
[Building Workflows](https://docs.agno.com/workflows/building-workflows)

Для Spine это подтверждает разделение недетерминированного agent execution и
durable workflow orchestration. Но `Team` не стоит включать в минимальный
`AgentProtocol`: устойчивый workflow-step должен запрашивать capability, а
внутреннее использование нескольких агентов может оставаться деталью конкретной
реализации этого capability.

## Удалённые агенты

`RemoteAgent` предоставляет похожий async API поверх двух транспортов:

- `protocol="agentos"` — AgentOS REST API;
- `protocol="a2a"` — A2A REST или JSON-RPC.

Оба режима поддерживают streaming и non-streaming `arun()`, но продолжение и
отмена доступны только через AgentOS protocol. Удалённые агенты можно
зарегистрировать в локальном AgentOS как gateway.[RemoteAgent reference](https://docs.agno.com/reference/agents/remote-agent)

AgentOS поднимает FastAPI runtime с execution API, persistence, auth, tracing и
операционными endpoints. Отдельные interfaces переводят AG-UI, A2A и события
мессенджеров во внутренний вызов Agent, Team или Workflow.
[AgentOS Runtime](https://docs.agno.com/agent-os/overview)
[AgentOS Interfaces](https://docs.agno.com/agent-os/interfaces/overview)

## Что перенять в Spine

Стоит перенять:

1. Structural `Protocol`, не требующий наследования от framework base class.
2. Композицию native agent из model adapter, instructions, tools и context ports.
3. Один нормализованный lifecycle: invoke, events, pause/resume, cancel и
   terminal result.
4. Возможность позднее добавить remote adapter, не меняя capability contract.
5. Явное различие dynamic agent/team coordination и durable workflow.

Не стоит переносить:

1. Большой универсальный конструктор `Agent` как публичный контракт Spine.
2. `Any`, `**kwargs` и union «финальный результат или event iterator» на доменной
   границе.
3. Session defaults и изменяемое runtime-состояние на объекте реализации агента.
4. Модельные, prompt-, memory-, RAG- и transport-настройки в
   `AgentInvocation`.
5. Возможность тихо вернуть строку при нарушении заявленной output schema.

## Принятые решения для Spine

Ниже отделены архитектурные решения Spine от фактов об устройстве Agno. Решения
согласованы после обсуждения результатов исследования.

### Назначение спецификации

Результатом проектирования должна стать архитектурная спецификация интерфейса
агентов, достаточная для последующей реализации. Непосредственная реализация SDK
и агентов в эту фазу не входит.

### Формы реализации

Текущая спецификация охватывает две локальные формы:

1. локальный объект, удовлетворяющий небольшому structural `AgentHandler`
   protocol;
2. обычную async-функцию, подключённую через adapter.

Внешние агенты, remote manifest и transport protocol пока не нужны. Runtime seam
не должен препятствовать будущему remote adapter, но выбор между собственным
HTTP protocol, AgentOS и A2A откладывается до появления подтверждённого кейса;
Agno не становится зависимостью Spine.

### Capability и Agent Version

Одна `AgentVersion` может предоставлять несколько связанных capabilities, но
каждый workflow step вызывает ровно один capability с отдельными типизированными
input/output schemas. Внутри package каждому capability соответствует отдельный
handler; универсальный строковый router внутри агента не используется.

### Форма локального handler

Локальная реализация удовлетворяет небольшому `Protocol`; наследование от
обязательного `BaseAgent` не требуется. Async-функции приводятся к тому же
interface отдельным adapter. Явный registry сопоставляет стабильный
`implementation_key` с factory; автоматическое сканирование модулей и
исполняемые import paths из конфигурации не используются.

### Вход и выход

Платформенный `AgentInvocation` содержит artifact IDs и lineage. Перед локальным
вызовом runtime загружает artifacts, проверяет capability schema и передаёт
handler типизированный `AgentRequest[InputT]`.

Handler возвращает типизированный `AgentResponse[OutputT]`. Runtime проверяет
его, сохраняет output artifacts, evidence и proposed actions, а также формирует
платформенный `AgentResult`. Реализация агента не зависит от Artifact Store и не
управляет persistence или idempotency самостоятельно.

### Состояние и вложенная координация

Реализация считается stateless между вызовами. В объекте допустимы неизменяемые
зависимости и безопасные технические caches; состояние run, workflow, approvals
и retries принадлежит платформе.

Одна реализация capability может временно координировать несколько внутренних
агентов. Такая координация остаётся implementation detail только пока она не
требует durable state. Значимые шаги, approvals, retries и handoffs типизированных
artifacts должны быть видимыми узлами Spine workflow.

### Events, ошибки и управление выполнением

Handler всегда возвращает один финальный response type. Progress, tool calls и
usage публикуются через ограниченный `EventSink`, а не через union финального
результата и event iterator.

Handler сообщает успешный или ожидаемый domain outcome, включая необходимость
review. Технические сбои нормализуются небольшой типизированной taxonomy; runtime
записывает попытку, а durable workflow выбирает retry, fallback, human review или
stop. Долговременные retry loops не скрываются внутри агента.

### Эталонные реализации

Спецификацию должны проверять два контрастных примера:

- `DocumentQAAgent` — объект с Context Broker, моделью, evidence и citations;
- `normalize_document` — детерминированная async-функция.

Эти примеры должны подтверждать, что один interface подходит и сложному
LLM-agent, и небольшой функции. При этом форма Python-кода не определяет тип
workflow step: детерминированная функция без Agent Binding является `code` step,
а function adapter используется для заменяемой версионированной реализации
capability с agent deployment lifecycle.
