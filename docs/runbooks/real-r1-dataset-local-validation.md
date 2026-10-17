# Локальная проверка real R1 dataset

`spine-eval-dataset-check` проверяет явно выбранный датасет и печатает только
метаданные. Команда не выполняет retrieval, не строит ответы и не оценивает
качество RAG. Synthetic dataset и его production contract не меняются.

После локальной подготовки approved dataset запустите из корня репозитория:

```bash
uv run spine-eval-dataset-check \
  --dataset-kind real \
  --dataset-dir data/private/r1_gold_draft \
  --source-root data/incoming
```

Для сохранения безопасного metadata-only отчёта добавьте
`--output /path/to/real-r1-validation.json`. Не сохраняйте его в tracked
директорию, если отчёт требуется держать локальным.

Для synthetic fixture выбор также обязателен:

```bash
uv run spine-eval-dataset-check --dataset-kind synthetic
```

Команда проверяет pinned version/digest, source file SHA-256, anonymized
representation SHA-256, revision/evidence/case links, citations, excerpts и
точные PDF page/character, DOCX paragraph/character и XLSX sheet/cell locators.
JSON остаётся источником кейсов; расхождение CSV явно отражается в отчёте и
блокирует проверку, не нормализуя ни один файл. Согласуйте export с владельцем
датасета, не редактируя approved JSON ради прохождения проверки.
При `validation_status: blocked` loader возвращает только validation report:
поле `dataset` отсутствует (`null`), поэтому caller не может использовать эти
кейсы, проигнорировав блокирующие findings.
При `--dataset-kind real` отсутствие любого входного локального файла приводит
к ошибке; автоматического перехода к synthetic dataset нет. Отчёт всегда
содержит `evaluation_status: not_run`.

На момент подготовки интеграции проверенная локальная копия имеет CSV/JSON
расхождение в `scenario_tags` у 4 кейсов и 53 XLSX citations, чьи excerpts не
совпадают со значениями указанного cell range. Loader сохранит JSON кейсы и
вернёт `validation_status: blocked` с агрегированными причинами; CLI выведет
metadata-only отчёт и завершится с кодом 2. JSON, CSV и evidence не
нормализуются автоматически.

Использовать real data можно только из локальных приватных директорий. Не
добавляйте оригиналы, anonymized representations, Q&A JSON/CSV, цитаты или
извлечённый текст в Git, CI artifacts, Issues, PR, логи или сторонние сервисы.
В CI оставляйте только маленькие безопасные synthetic contract fixtures.

## Ограничение end-to-end evaluation

У существующего offline runner есть production-contract observation seam, но в
приложении ещё нет production observer, который извлекает реальные наблюдения
и связывает workspace, principal и source revisions с реальными кейсами.
Передавать golden answer как observation запрещено. Поэтому эта команда
подтверждает целостность и пригодность формата, но не является R1 evaluation.
Полноценный прогон зависит от отдельного blocker [#443](https://github.com/alkselsv/spine/issues/443):
реализации и квалификации production application observer. Первый этап
интеграции остаётся local-first; удалённое приватное CI-хранилище не входит в
scope.
