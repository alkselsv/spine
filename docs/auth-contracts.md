# Identity and request-authorization contracts

Модуль `spine.auth` задаёт framework-independent seam для аутентификации и
авторизации защищённых R1-команд и запросов. Он не подключает OIDC, FastAPI или
PostgreSQL и не меняет legacy endpoints.

## Публичные контракты

- `AuthenticationAlias` содержит только точные provider `issuer` и `subject`.
  Alias не является Canonical Human Identity и не предоставляет роль или scope.
- `AuthenticatedAlias` добавляет только версию серверной конфигурации,
  подтвердившей credential. Произвольные token claims через seam не проходят.
- `RequestedAuthorizationScope` — не authority, а явный selector пары Workspace
  и Environment, которую directory обязана проверить.
- `RouteAuthorizationPolicy` — статическая серверная policy с единственной R1
  ролью `administrator`, обязательным Workspace/Environment scope, purpose,
  operation и опциональным server-selected service principal.
- `AuthorizationResolution` — один текущий immutable snapshot разрешённой
  Canonical Human Identity, tenant scope, роли и authorization generation.
- `AuthorizedRequestContext[DiagnosticContextT]` резервирует один типизированный
  slot для канонического Diagnostic Context из отдельного diagnostics seam.
  Это sealed read-only shape без публичного конструктора, issuer или verifier:
  caller не может создать или подменить экземпляр. Trusted implementation,
  provenance, issuance, verification, lifecycle и mapping в
  `TrustedPersistenceContext` принадлежат infrastructure composition root.

`AuthenticationPort.authenticate` и
`AuthorizationDirectory.resolve_request_authority` — purpose-specific ports.
Они намеренно не предоставляют CRUD или перечисление identity/membership
записей.

## Реестр и in-memory adapter

`RoutePolicyRegistry` хранит detached validated policy для каждого защищённого
route ID. Дубликат, отсутствующая policy, неизвестный route ID или policy,
созданная в обход validation, останавливают регистрацию.

`InMemoryAuthorizationDirectory` предназначен для application- и contract-тестов.
Он проверяет active alias и Canonical Human Identity, Workspace membership,
Environment ownership и membership, Environment-local role binding и текущую
authorization generation. Unknown, disabled, missing, stale и tenant-mismatch
состояния возвращают одну disclosure-safe категорию `AuthorizationDeniedError`.
Недоступность directory остаётся отдельной
`AuthorizationUnavailableError`.

Auth failures несут стабильные machine-readable `category` и `retryability`,
которые последующий Structured Error mapper переносит без разбора текста
исключения.

Document Access Policy остаётся за следующим vertical slice спецификации #7;
HTTP mapping и local development mode описаны ниже.

## Trusted request composition

`RequestAuthorizationComposer` связывает route ID с policy из
`RoutePolicyRegistry` до появления request-controlled данных. Полученный
`BoundRouteAuthorization` принимает только opaque credential, Workspace /
Environment selector и существующий `DiagnosticContext`; purpose, operation,
service principal и context issuer в request API отсутствуют.

Внутри одного async request lifecycle composition выполняет следующую
последовательность:

1. аутентифицирует detached credential через `AuthenticationPort`;
2. получает ровно один current snapshot из `AuthorizationDirectory`;
3. сверяет scope, role и authentication configuration version с теми же
   detached входами и bound route policy;
4. фиксирует actor, scope, roles, authorization generation, configuration
   version, purpose, operation, optional service principal и Diagnostic Context
   в одном подписанном immutable snapshot;
5. отображает этот snapshot в `TrustedPersistenceContext` через существующий
   `TrustedContextBoundary`;
6. коммитит registry-valid `access.decision` allow Audit Event и только после
   этого возвращает `AuthorizedRequest` application handler-у.

`TrustedRequestContextBoundary` устанавливается как verifier общей persistence
Unit of Work. Для interactive context он дополнительно проверяет object identity,
подпись request snapshot, exact field parity, owning `asyncio.Task` и незакрытый
request lifecycle. Поэтому копия/подмена context, foreign issuer, перенос в
другую task и повторное использование после выхода из `async with` отвергаются
до открытия repository. Worker context по-прежнему проверяется исходным
`TrustedContextBoundary` и не получает interactive authority.

Authentication/authorization failure до появления доверенных canonical actor и
tenant создаёт только unscoped `failure.observed` Diagnostic Event. Если actor и
scope уже установлены, но detached resolution не совпал с authentication
configuration provenance, отказ фиксируется tenant-scoped Audit Event. Поломка
обязательного allow audit не выдаёт context; поломка deny audit не превращает
отказ в разрешение. Request composition использует `access.decision` schema v2:
payload содержит только purpose, operation, authorization generation и
authentication configuration version. Совместимая schema v1 остаётся
зарегистрированной для существующих producers; issuer, subject, credential,
request body и protected target в события не попадают.

## Production OIDC adapter

`spine.infrastructure.auth.OIDCAuthenticationRuntime` реализует production
resource-server adapter поверх `AuthenticationPort`. Runtime владеет одним
лениво используемым async HTTP client, discovery/JWKS cache и refresh lock.
Composition root обязан вызвать `startup()` согласно `readiness_policy`, передать
приложению только `runtime.authentication` и закрыть runtime через `aclose()` или
async context manager.

`OIDCAuthenticationSettings` неизменяемы и версионированы. Каждая запись
`InteractiveClientQualification` является operator assertion для конкретного
client ID, exact issuer, audience и configuration version: это user-facing
client и client-credentials grant для Spine audience на стороне provider
отключён. R1 принимает только явно квалифицированный `RS256`; расширение
algorithm allowlist требует отдельной signed-token/JWK matrix. Readiness
публикует только status, безопасный code, configuration version, issuer origin и
freshness cache; client IDs, evidence references, keys и provider payload в него
не попадают.

Offline conformance harness расположен в `tests/infrastructure/auth/`: injected
transport и clock проверяют discovery, JWKS, подписанные tokens, rotation,
concurrent refresh, stale-key outage и leak corpus без внешнего provider.

Перед production-включением остаётся обязательной отдельная live qualification
точной deployment-конфигурации. Оператор должен подтвердить:

- exact issuer metadata и same-origin HTTPS `jwks_uri` без redirects;
- выпуск RFC 9068 `at+jwt` для exact Spine audience каждым разрешённым client;
- невозможность получить client-credentials token для этой audience каждым из
  этих clients;
- реальную ротацию signing key, expiry/skew behavior и сетевые timeout/failure
  режимы.

Результат live qualification должен ссылаться на ту же
`configuration_version`, что загружена в runtime. Ни один реальный issuer или
client registration в рамках offline-реализации тикета #71 не проверялся.

## FastAPI boundary и local development mode

`spine.api.authorization` добавляет защищённые `/api/v1` routes только через
`add_protected_api_route`. Регистрация связывает route ID с одной static policy
до запроса, устанавливает Bearer security и обязательные UUID-заголовки
`X-Workspace-ID` / `X-Environment-ID` в OpenAPI и выполняет authentication,
directory resolution и required audit до FastAPI dependency graph и handler.
`get_bearer_credential` является общей Bearer dependency, а
`get_authorized_request_context` после авторизации возвращает только sealed
context текущего запроса. Подмена context dependency через
`app.dependency_overrides` для защищённых routes отфильтровывается. Credential
проверяется до разбора scope, поэтому invalid credential остаётся safe `401`,
даже если Workspace/Environment отсутствуют или malformed.

Startup validation требует ровно одну классификацию для каждого `/api/v1`
route. Единственные public exemptions — точные route objects
`/api/v1/health/live` и `/api/v1/health/ready`; совпадающее имя нового route или
подключение нового router не наследует exemption. Legacy `/health`, `/ask` и
`/ingest` остаются вне R1 protected surface и сохраняют прежние response bodies.

`LocalAuthenticationSettings` и `LocalAuthenticationAdapter` предназначены
только для явного `development` deployment. Adapter повторно валидирует
настройки при создании, сравнивает один server-configured bearer в constant time
и возвращает отдельно настроенный Authentication Alias. Затем тот же
`AuthorizationDirectory` — в production composition это
`PostgreSQLAuthorizationDirectory` — проверяет identity, membership, role и
scope. Bearer не содержит и не выбирает actor, Workspace, Environment или role.

`compose_local_api_authorization` является deployable composition root для этого
режима: он fail-closed загружает или повторно валидирует local settings, создаёт
local adapter и всегда связывает его с `PostgreSQLAuthorizationDirectory`.
Внешний identity provider local режиму не нужен. Собранный runtime также даёт
health endpoint безопасный auth-readiness probe; protected runtime без такого
probe создать нельзя.

Generator-facing OpenAPI handoff описан в `web/api/README.md`. Protected
operations публикуют стабильный server-owned `operationId`, Bearer security и
два required UUID header; ручные файлы в `web/api/generated/` не создаются.

Local-настройки обязательны и не имеют fail-open defaults:

```text
SPINE_LOCAL_AUTH_DEPLOYMENT_MODE=development
SPINE_LOCAL_AUTH_CONFIGURATION_VERSION=local-2026-10-11
SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_ISSUER=spine-local://development
SPINE_LOCAL_AUTH_AUTHENTICATION_ALIAS_SUBJECT=configured-local-administrator
SPINE_LOCAL_AUTH_BEARER_CREDENTIAL=<random-secret-at-least-32-characters>
```

`staging`, `production`, отсутствующие или malformed version, alias и credential
останавливают создание local adapter. Значение credential нельзя публиковать в
readiness, логах, traces, errors или committed configuration.
