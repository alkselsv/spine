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

HTTP mapping, local development mode и document Access Policy входят в
следующие тикеты спецификации #5 и не являются частью этого модуля.

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
