# Локальная проверка real R1 dataset

`spine-eval-dataset-check` проверяет явно выбранный датасет и печатает только
метаданные. Команда не выполняет retrieval, не строит ответы и не оценивает
качество RAG. Synthetic dataset и его production contract не меняются.

После локальной подготовки approved dataset запустите из корня репозитория:

```bash
uv run spine-eval-dataset-check \
  --dataset-kind real \
  --dataset-dir data/private/r1_gold_approved/0.3.9-user-approved \
  --source-root data/incoming
```

Для сохранения безопасного metadata-only отчёта добавьте
`--output /path/to/real-r1-validation.json`. Не сохраняйте его в tracked
директорию, если отчёт требуется держать локальным.

Для synthetic fixture выбор также обязателен:

```bash
uv run spine-eval-dataset-check --dataset-kind synthetic
```

Для утверждённой версии loader проверяет pinned version/digest, source file
SHA-256, extracted representation and its SHA-256, revision/evidence/case links,
citations, excerpts и точные PDF page/character, DOCX paragraph/character и
XLSX sheet/cell locators. Проверка anonymized representations — часть
фактического контракта этих утверждённых версий датасета, а не универсальное
требование анонимизировать все benchmark datasets.
JSON остаётся источником кейсов; расхождение CSV явно отражается в отчёте и
блокирует проверку, не нормализуя ни один файл. Согласуйте export с владельцем
датасета, не редактируя approved JSON ради прохождения проверки.
При `validation_status: blocked` loader возвращает только validation report:
поле `dataset` отсутствует (`null`), поэтому caller не может использовать эти
кейсы, проигнорировав блокирующие findings.
При `--dataset-kind real` отсутствие любого входного локального файла приводит
к ошибке; автоматического перехода к synthetic dataset нет. Отчёт всегда
содержит `evaluation_status: not_run`.

The current approved local baseline is `0.3.9-user-approved`, pinned to digest
`7c2787272fd34643ff5168a46f5672b5a091f3070888546fa7b6ed0c918b3a62`. It has
149 cases: 136 answered, 13 abstained, and 0 denied, across 30 source documents.
Direct abstention context is XLSX-only (13); there are no direct PDF or DOCX
abstention cases and no denied cases, so those outcome slices have no benchmark
coverage. The earlier approved `0.3.8-user-approved` baseline remains an
immutable historical version (136 answered, 0 abstained, 0 denied).

For count interpretation, the approved corpus contains 171 unique source
evidence records, 177 case-citation references, and 177 locator references.
There are 158 claim-evidence relationships and 19 supplementary citation
references not linked to a claim. These counts use different units: 171 is the
deduplicated source evidence inventory; 177 counts citation occurrences across
cases (each with a locator); 158 counts claim-to-evidence links; and 19 counts
case citations absent from all expected claim evidence lists. Abstained cases
have no expected evidence or citations. The 136 answered case records are
unchanged from 0.3.8.

The loader validates source/evidence integrity only; it does not run RAG quality
evaluation. A metadata validation report always has `evaluation_status:
not_run`; this does not indicate a real RAG evaluation result.

## Canonical corpus preparation and readiness

After structural validation, the local benchmark composition may use
`ApprovedCorpusPreparationService` with the validated private
`r1-runtime-mapping-manifest:v1`, an issued `AuthorizedRequestContext`, and an
artifact adapter for the approved local files. The service accepts only the
pinned 30-source dataset identity; workspace and environment come from the
trusted context, never from operator-supplied selectors. The root-confined
`FilesystemApprovedCorpusArtifactStore` is the concrete adapter for an
explicitly registered set of local files. The service sends every revision
through `DocumentIngestionOrchestrator`;
it never writes to Cognee or another vector index. Its stable idempotency key is
derived from the manifest and approved ingestion intent, so a retry reuses the
same logical source and canonical revision lifecycle. A `prepared` result means
that canonical ingestion accepted the revision and durably requested projection;
it does not mean that projection is active.

After the normal projection worker and publication path have completed, convert
the verified `r1-runtime-revision-binding:v1` records with
`corpus_readiness_request_from_bindings(...)` and call
`CanonicalCorpusReadinessService.inspect(...)` using the issued trusted
workspace/environment context. A corpus is `ready` only when every requested
revision is accepted and current, has completed canonical projection processing,
is the exact included member of the active healthy/current snapshot, and the
existing authorized retrieval-scope resolver returns exactly the requested
canonical revision set. Missing, rejected, stale, tombstoned, excluded,
degraded, cross-scope, or unavailable state returns an actionable non-ready
reason without running Q&A.

The preparation report, `r1-corpus-readiness:v1` report, production observation,
quality evaluation, and release qualification are separate records. Preparation
and readiness therefore keep observation/evaluation/release fields at
`not_run`; do not infer later-stage success from them.

Использовать real data можно только из локальных приватных директорий. Не
добавляйте оригиналы, anonymized representations, Q&A JSON/CSV, цитаты или
извлечённый текст в Git, CI artifacts, Issues, PR, логи или сторонние сервисы.
В CI оставляйте только маленькие безопасные synthetic contract fixtures.

## Production observer integration

`DurableProductionQAAFlow` связывает `ProductionContractObserver` с
`DurableAskComposition`: он повторно разрешает authority, регистрирует
одноразовый capture до admission, использует канонический waiter и принимает
typed capture только от совпавшего execution identity. Он не вызывает retrieval
или capability отдельно и не восстанавливает отсутствующие данные из run history.

Реальная квалификация остаётся явным локальным действием владельца production
composition. В factory, переданной через `--composition` в
`spine-eval-real-benchmark`, настройте `RealBenchmarkRunner` на production
observer, который вызывает `DurableProductionQAAFlow`; fake flow разрешён только
для deterministic contract tests. Запускайте команду с approved local dataset,
manifest и source root, когда включены PostgreSQL, trusted authorization,
Temporal/worker и аутентифицированный CaptureChannel. Эта команда не запускается
в CI: private corpus и credentials остаются локальными.

Если runtime composition или любая из этих зависимостей не настроена, результат
должен быть safe `unavailable`/`failed`; fake успех не считается production
qualification. Подготовка и readiness остаются отдельными этапами до observer,
а evaluator не получает gold labels до финализации валидного observation.
