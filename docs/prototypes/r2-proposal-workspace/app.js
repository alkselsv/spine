/*
 * DISPOSABLE RESEARCH PROTOTYPE — Issue #588.
 * Two structurally different variants on one static route, switchable with
 * ?variant=guided and ?variant=workspace. No production API or persistence.
 */

const scenario = {
  customer: 'ООО «Север»',
  project: 'Интеллектуальный поиск по внутренней базе знаний',
  employees: 120,
  files: [
    { name: 'Запрос клиента.pdf', meta: 'PDF · 2 страницы · rev-src-017', status: 'Принят' },
    { name: 'Анкета информационной безопасности.xlsx', meta: 'XLSX · 34 строки · rev-src-018', status: 'Принят' },
  ],
  requirements: [
    'Локальное развёртывание в инфраструктуре заказчика.',
    'Доступ для 120 сотрудников с корпоративной аутентификацией.',
    'Поиск по PDF, DOCX, XLSX, Markdown и TXT.',
    'Ответы должны содержать ссылки на исходные документы.',
    'Разграничение доступа к документам по группам сотрудников.',
    'Поддержка русского языка для запросов и ответов.',
    'Журналирование запросов без раскрытия защищённого содержимого.',
    'Ежедневное обновление индекса знаний в согласованное окно.',
  ],
};

const guidedSteps = ['Запрос', 'Материалы', 'Требования', 'Уточнения', 'Аналоги', 'Смета', 'Черновик', 'Версии'];
const workspaceStages = ['Запрос', 'Требования', 'Аналоги', 'Смета', 'Черновик', 'Утверждение'];
const stateNames = ['ready', 'loading', 'empty', 'partial', 'stale', 'unavailable', 'denied', 'degraded', 'failed', 'cancelled', 'conflict', 'outdated'];

const appState = {
  variant: new URLSearchParams(location.search).get('variant') === 'guided' ? 'guided' : 'workspace',
  guidedStep: 0,
  workspaceStage: 1,
  uiState: 'ready',
  inspector: null,
  modal: null,
  editedRequirement: false,
  sla: '',
  migration: '',
  quantity: 120,
  draftEdited: false,
  version: 4,
  approved: false,
  lastFocus: null,
};

const app = document.querySelector('#app');
const liveRegion = document.querySelector('#live-region');

function announce(message) {
  liveRegion.textContent = '';
  requestAnimationFrame(() => { liveRegion.textContent = message; });
}

function esc(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
}

function shell(content) {
  const label = appState.variant === 'guided' ? 'A · Guided Review Wizard' : 'B · Staged Artifact Workspace';
  return `
    <div class="prototype-banner">
      <span><strong>Disposable prototype · Issue #588</strong> — демонстрация, не production UI и не утверждённый дизайн</span>
      <span>Все данные синтетические</span>
    </div>
    <header class="shell-header">
      <div><div class="brand">Spine Control Plane</div><h1>Коммерческие предложения</h1><div class="shell-subtitle">${label}</div></div>
      <div class="scope" aria-label="Текущий контекст"><span>Workspace: <b>sever-demo</b></span><span>Environment: <b>staging</b></span></div>
    </header>
    ${content}
    ${prototypeSwitcher()}
    ${overlay()}
  `;
}

function prototypeSwitcher() {
  const other = appState.variant === 'guided' ? 'workspace' : 'guided';
  const label = appState.variant === 'guided' ? 'A · Guided Wizard' : 'B · Staged Workspace';
  return `<nav class="prototype-switcher" aria-label="Переключение вариантов прототипа">
    <button type="button" data-action="switch" data-variant="${other}" aria-label="Предыдущий вариант">←</button>
    <a href="?variant=${appState.variant}" aria-current="page">${label}</a>
    <button type="button" data-action="switch" data-variant="${other}" aria-label="Следующий вариант">→</button>
  </nav>`;
}

function pageHeader() {
  return `<header class="page-header">
    <div><p class="eyebrow">КП-2026-0042 · ${scenario.customer}</p><h2>${esc(scenario.project)}</h2><p class="lede">Локальная система для ${scenario.employees} сотрудников · владелец: Анна Петрова · внешняя отправка отключена</p></div>
    <div class="header-meta"><span class="pill slice-badge">Первый slice</span><span class="pill running">run-pr-042 · ожидает человека</span><span class="pill">RequirementsArtifact v4</span></div>
  </header>`;
}

function stateLab() {
  return `<details class="state-lab">
    <summary>Лаборатория состояний — действия и восстановление</summary>
    <p class="help">Состояние применяется к текущему экрану. Проверяйте доступность действий и отсутствие protected content.</p>
    <div class="state-buttons" role="group" aria-label="Демонстрационное состояние">
      ${stateNames.map((name) => `<button type="button" data-action="state" data-state="${name}" aria-pressed="${appState.uiState === name}">${stateLabel(name)}</button>`).join('')}
    </div>
    <div class="state-panel">${statePanel()}</div>
  </details>`;
}

function stateLabel(state) {
  return ({ ready: 'Готово', loading: 'Loading / running', empty: 'Empty', partial: 'Partial', stale: 'Stale', unavailable: 'Evidence unavailable', denied: 'Permission denied', degraded: 'Degraded', failed: 'Failed', cancelled: 'Cancelled', conflict: '409 conflict', outdated: 'Outdated approval' })[state];
}

function statePanel() {
  const panels = {
    ready: `<div class="notice success"><strong>Текущая версия доступна.</strong> Можно редактировать и принять решение.</div>`,
    loading: `<div class="notice" role="status"><strong>extract_requirements выполняется.</strong> Обработано 2 из 2 материалов; проверяется evidence. Принятие заблокировано.</div><div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div>`,
    empty: `<div class="notice"><strong>Запрос ещё не создан.</strong> Добавьте клиента, описание и хотя бы один материал. <button class="link-button" data-action="recover">Создать запрос</button></div>`,
    partial: `<div class="notice warning"><strong>Частичный результат: 7 из 8 требований.</strong> Анкета обработана, одна страница PDF пропущена. Доступны просмотр и повтор; подтверждение заблокировано. <button class="link-button" data-action="recover">Повторить пропущенное</button></div>`,
    stale: `<div class="notice warning"><strong>Открыта v3, доступна v4.</strong> Редактирование и утверждение заблокированы до обновления. <button class="link-button" data-action="recover">Открыть v4</button></div>`,
    unavailable: `<div class="notice danger"><strong>Evidence недоступно.</strong> Содержимое скрыто; известен только безопасный код EVIDENCE_REVOKED. Требование нельзя подтвердить. <button class="link-button" data-action="recover">Запросить доступ</button></div>`,
    denied: `<div class="notice danger"><strong>Недостаточно прав.</strong> Защищённое название, locator и excerpt не отображаются. Scope: sever-demo / staging · trace tr-safe-91. <button class="link-button" data-action="recover">Вернуться к списку</button></div>`,
    degraded: `<div class="notice warning"><strong>Live updates недоступны.</strong> Сохранённый artifact остаётся доступен; включён polling каждые 15 секунд. Полнота evidence не подтверждена. <button class="link-button" data-action="recover">Обновить сейчас</button></div>`,
    failed: `<div class="notice danger"><strong>Этап extract_requirements завершился с ошибкой.</strong> v3 сохранена только для чтения. Новый retry создаст связанный run. <button class="link-button" data-action="recover">Создать новый run</button></div>`,
    cancelled: `<div class="notice"><strong>Run отменён Анной Петровой в 14:32.</strong> Это terminal state; v3 доступна только для чтения. <button class="link-button" data-action="recover">Повторить новым run</button></div>`,
    conflict: `<div class="notice danger"><strong>Конфликт optimistic locking.</strong> Олег изменил RequirementsArtifact v3 → v4. Локальные изменения не сохранены. <button class="link-button" data-action="recover">Сравнить и перенести изменения</button></div>`,
    outdated: `<div class="notice danger"><strong>Нельзя утвердить v3.</strong> Кандидат superseded версией v4; кнопка approval заблокирована. <button class="link-button" data-action="recover">Проверить diff v3 → v4</button></div>`,
  };
  return panels[appState.uiState];
}

function consequentialDisabled() {
  return appState.uiState !== 'ready' || !appState.sla || !appState.migration;
}

function guardedContent(content) {
  if (appState.uiState === 'denied') {
    return `<section aria-label="Доступ запрещён">${statePanel()}</section>`;
  }
  return content;
}

function guidedNextDisabled() {
  if (appState.guidedStep === 7 || appState.uiState !== 'ready') return true;
  if (appState.guidedStep === 2 && !appState.editedRequirement) return true;
  if (appState.guidedStep === 3 && (!appState.sla || !appState.migration)) return true;
  return false;
}

function guided() {
  const title = guidedSteps[appState.guidedStep];
  return shell(`<main id="main" class="page" tabindex="-1">
    ${pageHeader()}
    <ol class="wizard-stepper" aria-label="Этапы подготовки предложения">
      ${guidedSteps.map((step, index) => `<li><button type="button" data-action="guided-step" data-index="${index}" ${index === appState.guidedStep ? 'aria-current="step"' : ''}><span class="step-number">${index + 1}</span><span>${step}</span></button></li>`).join('')}
    </ol>
    <div class="wizard-layout">
      <section class="card wizard-main" aria-labelledby="guided-heading">
        <div class="card-header"><div><p class="eyebrow">Шаг ${appState.guidedStep + 1} из 8</p><h2 id="guided-heading" tabindex="-1">${title}</h2></div><span class="pill ${appState.guidedStep < 4 ? 'warning' : ''}">${appState.guidedStep < 4 ? 'Human gate' : 'Полный R2 demo'}</span></div>
        ${guardedContent(guidedContent(appState.guidedStep))}
        <footer class="wizard-footer">
          <button class="button secondary" type="button" data-action="guided-prev" ${appState.guidedStep === 0 ? 'disabled' : ''}>← Назад</button>
          <button class="button" type="button" data-action="guided-next" ${guidedNextDisabled() ? 'disabled' : ''}>${appState.guidedStep === 2 ? 'Сохранить и к уточнениям' : 'Сохранить и продолжить'} →</button>
        </footer>
      </section>
      <aside class="wizard-summary" aria-label="Контекст и статус">
        ${summaryCard()}
        <section class="card compact"><h3>Не потерять контекст</h3><ul class="summary-list"><li>RequirementsArtifact v4</li><li>2 source revisions</li><li>2 незакрытых уточнения</li><li><button class="link-button" data-action="runs">Связанные agent runs →</button></li></ul></section>
      </aside>
    </div>
    ${stateLab()}
  </main>`);
}

function guidedContent(step) {
  if (step === 0) return requestForm();
  if (step === 1) return materials();
  if (step === 2) return requirementsReview(true);
  if (step === 3) return clarifications();
  if (step === 4) return analogues();
  if (step === 5) return estimate();
  if (step === 6) return draft();
  return approval();
}

function workspace() {
  return shell(`<main id="main" class="page" tabindex="-1">
    ${pageHeader()}
    <div class="workspace">
      <nav class="stage-rail" aria-label="Артефакты и этапы">
        <h3>Этапы</h3>
        <ol class="stage-list">
          ${workspaceStages.map((stage, index) => `<li><button type="button" class="stage-button" data-action="workspace-stage" data-index="${index}" ${index === appState.workspaceStage ? 'aria-current="page"' : ''}><span class="stage-dot ${index === 0 ? 'done' : index === 1 ? 'attention' : ''}">${index === 0 ? '✓' : index + 1}</span><span class="stage-copy"><strong>${stage}</strong><small>${stageStatus(index)}</small></span></button></li>`).join('')}
        </ol>
        <div class="run-summary"><span class="pill running">run-pr-042</span><p>Пауза: review_requirements</p><button class="link-button" data-action="runs">Все связанные runs</button></div>
      </nav>
      <section class="workspace-center" aria-labelledby="artifact-heading">
        <div class="card-header"><div><p class="eyebrow">Активный typed artifact</p><h2 id="artifact-heading" tabindex="-1">${workspaceStages[appState.workspaceStage]}</h2></div><div class="action-row"><span class="pill">v${appState.version}</span><button class="button secondary mobile-inspector-button" type="button" data-action="evidence">Evidence</button></div></div>
        ${guardedContent(workspaceContent(appState.workspaceStage))}
      </section>
      <aside class="workspace-inspector" aria-label="Evidence, версия и контекст"><div class="sticky">${inspectorPanel()}</div></aside>
    </div>
    ${stateLab()}
  </main>`);
}

function stageStatus(index) {
  return ['готово', 'нужен review', '2 найдено', 'пересчитано', 'v4 черновик', 'ожидает решения'][index];
}

function workspaceContent(stage) {
  if (stage === 0) return `${requestForm()}${materials()}`;
  if (stage === 1) return `${requirementsReview(false)}${clarifications()}<div class="action-row" style="margin-top:1rem"><button class="button secondary" type="button" data-action="rework">Запросить доработку</button><button class="button" type="button" data-action="approve-requirements" ${consequentialDisabled() ? 'disabled' : ''}>Подтвердить RequirementsArtifact v4</button></div>`;
  if (stage === 2) return analogues();
  if (stage === 3) return estimate();
  if (stage === 4) return draft();
  return approval();
}

function requestForm() {
  return `<div class="field-grid">
    <div class="field"><label for="customer">Клиент</label><input id="customer" value="ООО «Север»" /></div>
    <div class="field"><label for="project">Проект</label><input id="project" value="Поиск по базе знаний" /></div>
    <div class="field wide"><label for="description">Описание запроса</label><textarea id="description">Локальная система интеллектуального поиска по внутренней базе знаний для 120 сотрудников.</textarea></div>
    <div class="field wide"><label for="constraints">Ограничения</label><textarea id="constraints">Развёртывание on-premise. Защищённые документы доступны только разрешённым группам.</textarea></div>
  </div><div class="notice"><strong>Scope закреплён:</strong> sever-demo / staging. Команда запуска idempotent и привязана к ClientRequest v2.</div>
  <div class="action-row"><button class="button secondary" type="button" data-action="upload">Добавить материалы</button><button class="button" type="button" data-action="run">Запустить извлечение требований</button></div>`;
}

function materials() {
  return `<section aria-labelledby="materials-heading"><div class="card-header"><div><h3 id="materials-heading">Материалы запроса</h3><p class="help">Каждый файл обрабатывается независимо и связывается с immutable SourceRevision.</p></div><span class="pill success">2 из 2</span></div><ul class="file-list">${scenario.files.map((file) => `<li class="file-row"><span class="file-meta"><strong>${file.name}</strong><small>${file.meta}</small></span><span class="pill success">${file.status}</span></li>`).join('')}</ul></section>`;
}

function requirementsReview(wizardMode) {
  return `<section aria-labelledby="requirements-heading"><div class="card-header"><div><h3 id="requirements-heading">8 извлечённых требований</h3><p class="help">Одно неоднозначно; два пункта требуют уточнения. Каждая правка создаёт новую версию.</p></div><span class="pill warning">1 ambiguity · 2 gaps</span></div>
    <ol class="requirements">${scenario.requirements.map((requirement, index) => requirementRow(requirement, index)).join('')}</ol>
    ${wizardMode ? '<div class="notice warning"><strong>Human gate:</strong> перед продолжением проверьте evidence, исправьте R8 и сохраните версию.</div>' : ''}</section>`;
}

function requirementRow(requirement, index) {
  const editing = index === 7 && appState.editedRequirement;
  return `<li class="requirement ${editing ? 'edited' : ''}"><span class="req-id">R${index + 1}</span><div class="req-copy">${editing ? `<div class="requirement-editor"><label class="sr-only" for="req-edit">Текст требования R8</label><textarea id="req-edit">Обновление индекса выполнять ежедневно с 02:00 до 04:00 МСК без остановки поиска.</textarea><small>Изменено менеджером · станет v5 после сохранения</small></div>` : `<p>${requirement}</p><small>${index === 4 ? 'Evidence: анкета ИБ · стр. 2' : index === 7 ? 'Неоднозначно: окно не указано' : 'Evidence проверено'}</small>`}</div><div class="req-actions"><button class="icon-button" type="button" data-action="evidence" aria-label="Открыть evidence для R${index + 1}">⌕</button>${index === 7 ? `<button class="icon-button" type="button" data-action="edit-requirement" aria-label="Редактировать R8">✎</button>` : ''}</div></li>`;
}

function clarifications() {
  return `<section style="margin-top:1rem" aria-labelledby="gaps-heading"><div class="card-header"><div><h3 id="gaps-heading">Уточнение информации</h3><p class="help">Ответы станут новой ревизией ClientRequest и будут связаны с новым run.</p></div><button class="button secondary" type="button" data-action="clarify">Открыть focused dialog</button></div><div class="gap-grid">
    <div class="gap"><p><strong>SLA поддержки</strong><br><span class="help">Blocking для сметы</span></p><label class="sr-only" for="sla">SLA поддержки</label><input id="sla" data-input="sla" value="${esc(appState.sla)}" placeholder="Например, 8×5" /></div>
    <div class="gap"><p><strong>Объём миграции</strong><br><span class="help">Blocking для состава работ</span></p><label class="sr-only" for="migration">Объём миграции</label><input id="migration" data-input="migration" value="${esc(appState.migration)}" placeholder="Например, 75 GB" /></div>
  </div></section>`;
}

function analogues() {
  return `<section><div class="card-header"><div><h3>Исторические аналоги</h3><p class="help">Аналог — evidence, но не актуальный источник цены.</p></div><span class="pill">2 результата</span></div><div class="analogue-grid"><article class="analogue"><span class="pill success">current</span><h3>«Полюс» · база знаний</h3><p>Сходство: on-premise, 150 пользователей, ролевой доступ. Утверждён 18.06.2026.</p><button class="link-button" data-action="evidence">Открыть evidence</button></article><article class="analogue stale"><span class="pill warning">stale</span><h3>«Тайга» · поиск документов</h3><p>Архивная версия 2023 года. Цены и security assumptions использовать нельзя.</p><button class="button secondary" type="button" disabled>Исключён из basis</button></article></div></section>`;
}

function estimate() {
  const total = appState.quantity * 1250 + 375000;
  return `<section><div class="card-header"><div><h3>Смета · Estimate v2</h3><p class="help">Редактируются только входы; итоги рассчитаны детерминированно. Синтетические цены.</p></div><span class="pill success">approved pricing basis PB-12</span></div><div class="card compact"><table class="estimate-table"><thead><tr><th>Позиция</th><th>Количество</th><th>Basis</th><th>Сумма</th></tr></thead><tbody><tr><td>Пользовательские лицензии</td><td><label class="sr-only" for="quantity">Количество лицензий</label><input id="quantity" data-input="quantity" type="number" min="1" value="${appState.quantity}" /></td><td>PB-12 rev.4</td><td>${(appState.quantity * 1250).toLocaleString('ru-RU')} ₽</td></tr><tr><td>Внедрение и миграция 75 GB</td><td>1 пакет</td><td>PB-12 rev.4</td><td>375 000 ₽</td></tr></tbody></table><div class="estimate-total"><span>Итого без НДС</span><span>${total.toLocaleString('ru-RU')} ₽</span></div></div><div class="notice success" role="status">Пересчёт выполнен серверным code step · Decimal · RUB · receipt calc-204.</div></section>`;
}

function draft() {
  return `<section><div class="card-header"><div><h3>Черновик КП · v4</h3><p class="help">Правки менеджера отделены от исходного agent draft.</p></div><button class="button secondary" type="button" data-action="diff">Сравнить v3 → v4</button></div><div class="document-preview"><p class="eyebrow">ООО «Север»</p><h2>Интеллектуальный поиск по базе знаний</h2><p>Предлагаем локальное решение для безопасного поиска по внутренним документам.</p><label class="field-label" for="draft-section">Раздел «Подход»</label><textarea id="draft-section" data-input="draft">${appState.draftEdited ? 'Система разворачивается в инфраструктуре заказчика. Индекс обновляется ежедневно с 02:00 до 04:00 МСК; поиск остаётся доступным.' : 'Система разворачивается в инфраструктуре заказчика и ежедневно обновляет индекс знаний.'}</textarea><p class="help">${appState.draftEdited ? 'Изменено менеджером · несохранённая правка' : 'Сгенерировано agent run run-pr-051'}</p><button class="button" type="button" data-action="save-draft">Сохранить новую версию</button></div></section>`;
}

function approval() {
  return `<section><div class="version-grid"><article class="card compact"><span class="pill danger">v3 отклонена</span><h3>ProposalDraft v3</h3><p class="muted">Причина: не указано окно обновления индекса. Решение: Сергей Волков · 15:20.</p><button class="button secondary" type="button" data-action="diff">Сравнить с v4</button></article><article class="card compact"><span class="pill ${appState.approved ? 'success' : 'warning'}">v4 ${appState.approved ? 'утверждена' : 'ожидает решения'}</span><h3>ProposalDraft v4</h3><p class="muted">Основание: RequirementsArtifact v4 · Estimate v2 · render receipt отсутствует.</p><button class="button" type="button" data-action="approval" ${appState.uiState !== 'ready' || appState.approved ? 'disabled' : ''}>${appState.approved ? 'Утверждено' : 'Проверить и утвердить v4'}</button></article></div><div class="notice"><strong>Human gate:</strong> approval привязан к точной immutable версии. Утверждение не отправляет документ клиенту.</div><button class="link-button" data-action="runs">Просмотреть связанные agent runs</button></section>`;
}

function summaryCard() {
  return `<section class="card compact"><h3>Первый implementation slice</h3><ul class="summary-list"><li>ClientRequest v2 ✓</li><li>normalize_request ✓</li><li>extract_requirements ✓</li><li>validate_evidence ✓</li><li><strong>review_requirements · human gate</strong></li><li>RequirementsArtifact v4</li></ul></section>`;
}

function inspectorPanel() {
  if (appState.uiState === 'denied') return `<h3>Source evidence</h3>${statePanel()}`;
  if (appState.uiState === 'unavailable') return `<h3>Source evidence</h3>${statePanel()}`;
  return `<div class="card-header"><div><p class="eyebrow">Source evidence</p><h3>R5 · разграничение доступа</h3></div><span class="pill success">current</span></div><blockquote class="source-quote">«Доступ к разделам базы знаний должен назначаться по группам корпоративного каталога».</blockquote><dl class="metadata"><dt>Источник</dt><dd>Анкета информационной безопасности</dd><dt>Revision</dt><dd>rev-src-018</dd><dt>Locator</dt><dd>sheet «Доступ», row 12</dd><dt>Проверено</dt><dd>current authorization · 14:18</dd></dl><button class="button secondary" type="button" data-action="evidence">Открыть инспектор</button><hr style="border:0;border-top:1px solid var(--border);margin:1rem 0"><h3>Версия и lineage</h3><ul class="summary-list"><li>RequirementsArtifact v4</li><li>ClientRequest v2</li><li>run-pr-042 → run-pr-047</li><li><button class="link-button" data-action="runs">Все runs</button></li></ul>`;
}

function overlay() {
  if (!appState.modal && !appState.inspector) return '';
  if (appState.inspector) return `<div class="drawer-backdrop" data-overlay="true"><aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><div class="dialog-header"><div><p class="eyebrow">Current-authorized inspection</p><h2 id="drawer-title" tabindex="-1">Source evidence</h2></div><button class="icon-button" type="button" data-action="close" aria-label="Закрыть инспектор">×</button></div>${inspectorPanel()}</aside></div>`;
  const content = modalContent(appState.modal);
  return `<div class="dialog-backdrop" data-overlay="true"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><div class="dialog-header"><div><p class="eyebrow">Focused human task</p><h2 id="dialog-title" tabindex="-1">${content.title}</h2></div><button class="icon-button" type="button" data-action="close" aria-label="Закрыть диалог">×</button></div>${content.body}</section></div>`;
}

function modalContent(kind) {
  if (kind === 'clarify') return { title: 'Ответить на уточнения', body: `${clarifications()}<div class="action-row" style="margin-top:1rem"><button class="button secondary" type="button" data-action="close">Сохранить черновик</button><button class="button" type="button" data-action="submit-clarification">Сохранить ответы и продолжить run</button></div>` };
  if (kind === 'diff') return { title: 'Semantic diff · v3 → v4', body: `<p class="muted">Сравнение фиксирует base/head, автора и причину. Изменения evidence требуют повторного review.</p><div class="diff"><div><strong>v3 · отклонена</strong><del>Индекс обновляется ежедневно.</del></div><div><strong>v4 · кандидат</strong><ins>Индекс обновляется ежедневно с 02:00 до 04:00 МСК без остановки поиска.</ins></div></div><div class="notice warning">Изменено требование R8 и раздел «Подход». Pricing basis не менялся.</div><div class="action-row"><button class="button secondary" type="button" data-action="close">Вернуться без решения</button><button class="button" type="button" data-action="close">Продолжить review v4</button></div>` };
  if (kind === 'approval') return { title: 'Утвердить ProposalDraft v4', body: `<div class="notice"><strong>Точная версия:</strong> ProposalDraft v4 · digest 81b7…ef3<br><strong>Upstream:</strong> RequirementsArtifact v4 · Estimate v2</div><div class="field"><label for="approval-comment">Комментарий решения</label><textarea id="approval-comment">Проверены требования, evidence и смета.</textarea></div><p class="help">Внешняя отправка не выполняется. Решение будет immutable receipt.</p><div class="action-row"><button class="button danger" type="button" data-action="reject">Отклонить</button><button class="button" type="button" data-action="approve-final">Утвердить ровно v4</button></div>` };
  if (kind === 'runs') return { title: 'Связанные agent runs', body: `<div class="run-grid"><article class="card compact"><span class="pill success">completed</span><h3>run-pr-042</h3><p class="help">normalize_request → extract_requirements → validate_evidence</p></article><article class="card compact"><span class="pill success">completed</span><h3>run-pr-047</h3><p class="help">Clarification received · RequirementsArtifact v4</p></article><article class="card compact"><span class="pill danger">rejected</span><h3>run-pr-051</h3><p class="help">ProposalDraft v3 · human decision receipt</p></article><article class="card compact"><span class="pill warning">waiting</span><h3>run-pr-054</h3><p class="help">ProposalDraft v4 · approval work item</p></article></div><button class="button secondary" type="button" data-action="close" style="margin-top:1rem">Вернуться в workspace</button>` };
  return { title: 'Запросить доработку', body: `<p>Создаст новый связанный run; текущая версия останется immutable.</p><div class="field"><label for="rework-comment">Что нужно изменить</label><textarea id="rework-comment">Уточнить окно обновления индекса.</textarea></div><div class="action-row"><button class="button secondary" type="button" data-action="close">Отмена</button><button class="button" type="button" data-action="close">Создать rework run</button></div>` };
}

function render(options = {}) {
  app.innerHTML = appState.variant === 'guided' ? guided() : workspace();
  if (options.focusHeading) {
    requestAnimationFrame(() => document.querySelector(options.focusHeading)?.focus());
  }
  if (appState.modal || appState.inspector) requestAnimationFrame(() => document.querySelector('[role="dialog"] h2')?.focus());
}

function setVariant(variant) {
  const url = new URL(location.href);
  url.searchParams.set('variant', variant);
  history.replaceState(null, '', url);
  appState.variant = variant;
  appState.modal = null;
  appState.inspector = null;
  render({ focusHeading: '#main' });
  announce(`Открыт вариант ${variant === 'guided' ? 'A, Guided Review Wizard' : 'B, Staged Artifact Workspace'}`);
}

function openOverlay(kind, inspector = false) {
  appState.lastFocus = document.activeElement;
  if (inspector) appState.inspector = kind;
  else appState.modal = kind;
  render();
}

function closeOverlay() {
  const returnFocus = appState.lastFocus;
  appState.modal = null;
  appState.inspector = null;
  render();
  requestAnimationFrame(() => returnFocus?.focus?.());
}

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]');
  if (!target) {
    if (event.target.matches('[data-overlay="true"]')) closeOverlay();
    return;
  }
  const action = target.dataset.action;
  if (action === 'switch') return setVariant(target.dataset.variant);
  if (action === 'guided-step') { appState.guidedStep = Number(target.dataset.index); render({ focusHeading: '#guided-heading' }); return announce(`Шаг ${appState.guidedStep + 1}: ${guidedSteps[appState.guidedStep]}`); }
  if (action === 'guided-prev' || action === 'guided-next') { appState.guidedStep += action === 'guided-next' ? 1 : -1; appState.guidedStep = Math.max(0, Math.min(7, appState.guidedStep)); render({ focusHeading: '#guided-heading' }); return; }
  if (action === 'workspace-stage') { appState.workspaceStage = Number(target.dataset.index); render({ focusHeading: '#artifact-heading' }); return announce(`Открыт артефакт: ${workspaceStages[appState.workspaceStage]}`); }
  if (action === 'state') { appState.uiState = target.dataset.state; render(); return announce(`Демонстрационное состояние: ${stateLabel(appState.uiState)}`); }
  if (action === 'recover') { appState.uiState = 'ready'; render(); return announce('Восстановлено корректное состояние'); }
  if (action === 'evidence') return openOverlay('evidence', true);
  if (action === 'close') return closeOverlay();
  if (action === 'clarify') return openOverlay('clarify');
  if (action === 'diff') return openOverlay('diff');
  if (action === 'approval') return openOverlay('approval');
  if (action === 'runs') return openOverlay('runs');
  if (action === 'rework') return openOverlay('rework');
  if (action === 'edit-requirement') { appState.editedRequirement = !appState.editedRequirement; render(); return; }
  if (action === 'submit-clarification') { appState.sla = appState.sla || '8×5'; appState.migration = appState.migration || '75 GB'; closeOverlay(); return announce('Уточнения сохранены; создана новая ревизия ClientRequest'); }
  if (action === 'save-draft') { appState.draftEdited = true; appState.version = 4; render(); return announce('Черновик сохранён как версия 4'); }
  if (action === 'approve-final') { appState.approved = true; closeOverlay(); return announce('ProposalDraft v4 утверждён. Внешняя отправка не выполнена.'); }
  if (action === 'reject') { closeOverlay(); return announce('Версия отклонена и возвращена автору'); }
  if (action === 'approve-requirements') { announce('RequirementsArtifact v4 подтверждён и сохранён'); return; }
  if (action === 'run') { appState.uiState = 'loading'; render(); return announce('Запущено извлечение требований'); }
  if (action === 'upload') return announce('В прототипе используются два заранее подготовленных синтетических файла');
});

document.addEventListener('input', (event) => {
  const input = event.target.dataset.input;
  if (input === 'sla') appState.sla = event.target.value;
  if (input === 'migration') appState.migration = event.target.value;
  if (input === 'draft') appState.draftEdited = true;
});

document.addEventListener('change', (event) => {
  if (event.target.dataset.input === 'quantity') {
    appState.quantity = Number(event.target.value) || 0;
    render({ focusHeading: '#artifact-heading' });
    announce('Смета пересчитана детерминированно');
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && (appState.modal || appState.inspector)) return closeOverlay();
  const tag = document.activeElement?.tagName;
  if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) && !document.activeElement?.isContentEditable) {
    event.preventDefault();
    setVariant(appState.variant === 'guided' ? 'workspace' : 'guided');
  }
  if (event.key === 'Tab' && (appState.modal || appState.inspector)) {
    const dialog = document.querySelector('[role="dialog"]');
    const focusable = [...dialog.querySelectorAll('button, input, textarea, select, a[href], [tabindex]:not([tabindex="-1"])')].filter((element) => !element.disabled);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

render();
