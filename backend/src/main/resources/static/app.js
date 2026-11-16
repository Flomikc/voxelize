/* Voxelize — клиентская логика. Чистый JS, без зависимостей и сборщиков.
 *
 * Поток: drag-and-drop -> POST /api/upload -> опрос GET /api/status/{id} (setInterval)
 *        -> статус DONE -> <model-viewer src="/api/model/{id}.glb">
 */
(() => {
  'use strict';

  // ---------- константы ----------
  const API = '/api';
  const LIMITS = { files: 20, bytes: 20 * 1024 * 1024 };   // синхронизировано с application.yml
  const POLL_MS = 1000;            // период опроса статуса
  const REQUEST_TIMEOUT_MS = 8000; // таймаут одного запроса статуса
  const MAX_POLL_ERRORS = 8;       // подряд неудачных опросов до признания связи потерянной
  const STORE_KEY = 'voxelize.task';
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

  // Этапы совпадают со значениями TaskStatus на бэкенде
  const STAGES = [
    { key: 'QUEUED',           label: 'В очереди',        text: 'Ожидание в очереди' },
    { key: 'UPLOADING',        label: 'Передача',         text: 'Передача задачи в ML-сервис' },
    { key: 'DEPTH_ESTIMATION', label: 'Анализ глубины',   text: 'Анализ глубины' },
    { key: 'MESH_GENERATION',  label: 'Генерация меша',   text: 'Генерация меша' },
    { key: 'TEXTURING',        label: 'Текстуры и экспорт', text: 'Наложение текстур' },
    { key: 'DONE',             label: 'Готово',           text: 'Готово' },
  ];

  const $ = (id) => document.getElementById(id);
  const el = {
    views: {
      upload: $('panel-upload'), progress: $('panel-progress'),
      result: $('panel-result'), error: $('panel-error'),
    },
    sys: $('sys'), sysText: $('sys-text'),
    dropzone: $('dropzone'), input: $('file-input'), notice: $('notice'),
    fileList: $('file-list'), modeHint: $('mode-hint'),
    btnStart: $('btn-start'), btnClear: $('btn-clear'),
    stepper: $('stepper'), pct: $('pct'), bar: $('bar'), barFill: $('bar-fill'),
    stageText: $('stage-text'), stageDetail: $('stage-detail'),
    elapsed: $('elapsed'), taskId: $('task-id'), log: $('log'), conn: $('conn'),
    errorText: $('error-text'), btnRetry: $('btn-retry'),
    viewer: $('viewer'), stage: $('viewer-stage'), loader: $('loader'), loaderBar: $('loader-bar'),
    viewerError: $('viewer-error'), resultMeta: $('result-meta'),
    btnRotate: $('btn-rotate'), btnReset: $('btn-reset'), exposure: $('exposure'),
    btnFullscreen: $('btn-fullscreen'), btnShot: $('btn-shot'), download: $('download'), btnNew: $('btn-new'),
  };

  const state = {
    files: [], nextId: 1, busy: false,
    taskId: null, startedAt: 0, pollTimer: null, clockTimer: null,
    inFlight: false, pollErrors: 0, lastLogKey: '', lastStageIdx: 0, elapsedAtFinish: '',
  };

  // ---------- утилиты ----------
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));
  const pad2 = (n) => String(n).padStart(2, '0');
  const fmtTime = (ms) => { const s = Math.max(0, Math.floor(ms / 1000)); return `${pad2(Math.floor(s / 60))}:${pad2(s % 60)}`; };
  const fmtBytes = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);

  async function fetchT(url, opts = {}, ms = REQUEST_TIMEOUT_MS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    try { return await fetch(url, { ...opts, signal: ctrl.signal }); }
    finally { clearTimeout(timer); }
  }
  const readJson = async (res) => { try { return await res.json(); } catch { return null; } };

  const store = {
    read() { try { return JSON.parse(localStorage.getItem(STORE_KEY)); } catch { return null; } },
    write(v) { try { localStorage.setItem(STORE_KEY, JSON.stringify(v)); } catch { /* приватный режим */ } },
    clear() { try { localStorage.removeItem(STORE_KEY); } catch { /* ignore */ } },
  };

  // ---------- переключение экранов ----------
  function setView(name, { scroll = true } = {}) {
    for (const [key, node] of Object.entries(el.views)) node.hidden = key !== name;
    document.body.dataset.view = name;
    const heading = el.views[name].querySelector('h2');
    heading?.focus({ preventScroll: true });
    if (scroll) el.views[name].scrollIntoView?.({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
  }

  // ---------- статус сервера ----------
  function setSys(stateName) {
    const labels = { up: 'СЕРВЕР: ONLINE', down: 'СЕРВЕР: OFFLINE', checking: 'СЕРВЕР: ПРОВЕРКА…' };
    el.sys.dataset.state = stateName;
    el.sysText.textContent = labels[stateName];
  }
  async function pingServer() {
    setSys('checking');
    try {
      const res = await fetchT('/actuator/health', { cache: 'no-store' }, 4000);
      const data = await readJson(res);
      setSys(res.ok && data?.status === 'UP' ? 'up' : 'down');
    } catch { setSys('down'); }
  }

  // ---------- выбор файлов ----------
  function isAllowed(file) {
    if (file.type === 'image/jpeg' || file.type === 'image/png') return true;
    return file.type === '' && /\.(jpe?g|png)$/i.test(file.name);   // некоторые ОС не отдают MIME
  }

  function showNotice(messages) {
    el.notice.replaceChildren();
    if (!messages.length) { el.notice.hidden = true; return; }
    const shown = messages.slice(0, 4);
    for (const m of shown) { const d = document.createElement('div'); d.textContent = m; el.notice.append(d); }
    if (messages.length > shown.length) {
      const d = document.createElement('div'); d.textContent = `…и ещё ${messages.length - shown.length}`; el.notice.append(d);
    }
    el.notice.hidden = false;
  }

  function addFiles(list) {
    const rejected = [];
    for (const file of list) {
      const sig = `${file.name}|${file.size}|${file.lastModified}`;
      if (state.files.length >= LIMITS.files) rejected.push(`Лимит ${LIMITS.files} файлов: «${file.name}» пропущен`);
      else if (!isAllowed(file)) rejected.push(`«${file.name}»: поддерживаются только JPG и PNG`);
      else if (file.size === 0) rejected.push(`«${file.name}»: файл пустой`);
      else if (file.size > LIMITS.bytes) rejected.push(`«${file.name}»: больше ${LIMITS.bytes / 1048576} МБ (${fmtBytes(file.size)})`);
      else if (state.files.some((f) => f.sig === sig)) rejected.push(`«${file.name}»: уже добавлен`);
      else state.files.push({ id: state.nextId++, sig, file, url: URL.createObjectURL(file) });
    }
    showNotice(rejected);
    renderFiles();
  }

  function removeFile(id) {
    const i = state.files.findIndex((f) => f.id === id);
    if (i < 0) return;
    URL.revokeObjectURL(state.files[i].url);
    state.files.splice(i, 1);
    showNotice([]);
    renderFiles();
  }

  function clearFiles() {
    state.files.forEach((f) => URL.revokeObjectURL(f.url));
    state.files = [];
    renderFiles();
  }

  function renderFiles() {
    el.fileList.replaceChildren();
    for (const f of state.files) {
      const li = document.createElement('li');
      const img = document.createElement('img'); img.src = f.url; img.alt = ''; img.loading = 'lazy';
      const info = document.createElement('div'); info.className = 'file-info';
      const name = document.createElement('span'); name.className = 'file-name'; name.textContent = f.file.name; name.title = f.file.name;
      const size = document.createElement('span'); size.className = 'file-size'; size.textContent = fmtBytes(f.file.size);
      info.append(name, size);
      const rm = document.createElement('button');
      rm.type = 'button'; rm.className = 'file-remove'; rm.textContent = '✕';
      rm.setAttribute('aria-label', `Убрать «${f.file.name}»`);
      rm.addEventListener('click', () => removeFile(f.id));
      li.append(img, info, rm);
      el.fileList.append(li);
    }
    const n = state.files.length;
    el.fileList.hidden = n === 0;
    el.btnClear.hidden = n === 0;
    el.btnStart.disabled = n === 0 || state.busy;
    renderModeHint(n);
  }

  function renderModeHint(n) {
    el.modeHint.replaceChildren();
    if (n === 0) { el.modeHint.hidden = true; return; }
    const label = document.createElement('b');
    const text = document.createElement('span');
    if (n === 1) {
      label.textContent = 'РЕЖИМ: single-view · TripoSR';
      text.textContent = ' — быстрее; обратную сторону объекта нейросеть достраивает сама. Лучше всего работает на однотонном фоне.';
    } else {
      label.textContent = `РЕЖИМ: multi-view · VGGT + Poisson (${n} фото)`;
      text.textContent = ' — точнее, но дольше и тяжелее. Снимайте объект с разных сторон (4–8 кадров) при ровном свете.';
      if (n > 6) text.textContent += ' Сервер может использовать не все кадры.';
    }
    el.modeHint.append(label, text);
    el.modeHint.hidden = false;
  }

  // ---------- отправка ----------
  async function submit() {
    if (!state.files.length || state.busy) return;
    state.busy = true;
    el.btnStart.disabled = true;
    el.btnStart.textContent = '… отправка';
    showNotice([]);

    const form = new FormData();
    state.files.forEach((f) => form.append('files', f.file, f.file.name));   // поле «files» ждёт UploadController

    try {
      const res = await fetch(`${API}/upload`, { method: 'POST', body: form });
      const data = await readJson(res);
      if (!res.ok || !data?.taskId) throw new Error(data?.error || `Ошибка сервера (HTTP ${res.status})`);
      beginTask(data.taskId, Date.now());
    } catch (e) {
      showNotice([e instanceof TypeError ? 'Нет связи с сервером. Проверьте, что бэкенд запущен.' : e.message]);
      pingServer();
    } finally {
      state.busy = false;
      el.btnStart.textContent = '▶ Запустить генерацию';
      renderFiles();
    }
  }

  // ---------- задача и опрос статуса ----------
  function beginTask(taskId, startedAt) {
    state.taskId = taskId;
    state.startedAt = startedAt;
    state.lastLogKey = '';
    state.lastStageIdx = 0;
    store.write({ id: taskId, startedAt });
    clearFiles();                       // превью больше не нужны — освобождаем память

    el.taskId.textContent = `task ${taskId.slice(0, 8)}`;
    el.log.replaceChildren();
    el.conn.hidden = true;
    buildStepper();
    setProgress(0);
    el.stageText.textContent = STAGES[0].text;
    el.stageDetail.textContent = '';
    logLine(`Задача ${taskId} принята сервером`);
    setView('progress');
    startClock();
    startPolling();
  }

  function startPolling() {
    stopPolling();
    state.pollErrors = 0;
    poll();                                            // первый опрос сразу
    state.pollTimer = setInterval(poll, POLL_MS);
  }
  function stopPolling() { clearInterval(state.pollTimer); state.pollTimer = null; }

  async function poll() {
    if (state.inFlight || !state.taskId) return;       // не накапливаем параллельные запросы
    state.inFlight = true;
    const id = state.taskId;
    try {
      const res = await fetchT(`${API}/status/${encodeURIComponent(id)}`, { cache: 'no-store' });
      if (id !== state.taskId) return;                 // пользователь уже начал другую задачу
      if (res.status === 404) { fail('Задача не найдена на сервере. Возможно, она была удалена по сроку хранения. Запустите генерацию заново.'); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.pollErrors = 0;
      el.conn.hidden = true;
      applyStatus(data);
    } catch {
      if (id !== state.taskId) return;
      state.pollErrors += 1;
      if (state.pollErrors >= MAX_POLL_ERRORS) {
        fail('Потеряна связь с сервером. Если он перезапускался, задача могла прерваться — запустите генерацию заново.');
      } else {
        el.conn.textContent = `Нет связи с сервером, повторяю… (${state.pollErrors}/${MAX_POLL_ERRORS})`;
        el.conn.hidden = false;
      }
    } finally {
      state.inFlight = false;
    }
  }

  function applyStatus(s) {
    const idx = STAGES.findIndex((st) => st.key === s.status);
    if (idx >= 0) state.lastStageIdx = idx;

    if (s.status === 'FAILED') { fail(s.error || 'Не удалось сгенерировать модель'); return; }

    const pct = clamp(Math.round(Number(s.progress) || 0), 0, 100);
    setProgress(s.status === 'DONE' ? 100 : pct);
    updateStepper(s.status);

    const stage = STAGES[idx] ?? STAGES[state.lastStageIdx];
    el.stageText.textContent = stage.text;
    el.stageDetail.textContent = s.message && s.message !== stage.text ? s.message : '';

    const key = `${s.status}|${s.message}`;
    if (key !== state.lastLogKey) {                    // в журнал — только смена сообщения
      state.lastLogKey = key;
      logLine(s.message || stage.text, s.status === 'DONE' ? 'ok' : '');
    }

    if (s.status === 'DONE') {
      stopPolling();
      stopClock();
      state.elapsedAtFinish = fmtTime(Date.now() - state.startedAt);
      el.barFill.classList.remove('running');
      const url = typeof s.modelUrl === 'string' && s.modelUrl.startsWith(`${API}/model/`)
        ? s.modelUrl : `${API}/model/${state.taskId}.glb`;
      setTimeout(() => { if (state.taskId) showResult(url); }, 500);   // дать увидеть 100%
    }
  }

  function fail(message) {
    stopPolling();
    stopClock();
    updateStepper('FAILED');
    logLine(message, 'err');
    store.clear();
    state.taskId = null;
    el.errorText.textContent = message;
    setView('error');
  }

  // ---------- UI прогресса ----------
  function buildStepper() {
    el.stepper.replaceChildren();
    STAGES.forEach((st, i) => {
      const li = document.createElement('li');
      li.className = 'step'; li.dataset.key = st.key;
      const n = document.createElement('span'); n.className = 'n'; n.textContent = pad2(i + 1);
      li.append(n, document.createTextNode(st.label));
      el.stepper.append(li);
    });
    el.barFill.classList.add('running');
  }

  function updateStepper(status) {
    const items = [...el.stepper.children];
    const idx = status === 'FAILED' ? state.lastStageIdx : STAGES.findIndex((s) => s.key === status);
    items.forEach((li, i) => {
      li.classList.remove('done', 'active', 'failed');
      if (status === 'FAILED') { if (i < idx) li.classList.add('done'); else if (i === idx) li.classList.add('failed'); }
      else if (status === 'DONE' || i < idx) li.classList.add('done');
      else if (i === idx) li.classList.add('active');
    });
  }

  function setProgress(pct) {
    el.barFill.style.width = `${pct}%`;
    el.pct.textContent = `${pct}%`;
    el.bar.setAttribute('aria-valuenow', String(pct));
  }

  function logLine(text, cls = '') {
    const line = document.createElement('div');
    line.className = `log-line ${cls}`.trim();
    const ts = document.createElement('span'); ts.className = 'ts'; ts.textContent = `[${fmtTime(Date.now() - state.startedAt)}]`;
    const msg = document.createElement('span'); msg.textContent = ` ${text}`;
    line.append(ts, msg);
    el.log.append(line);
    while (el.log.childElementCount > 80) el.log.firstElementChild.remove();
    el.log.scrollTop = el.log.scrollHeight;
  }

  function startClock() {
    stopClock();
    const tick = () => { el.elapsed.textContent = fmtTime(Date.now() - state.startedAt); };
    tick();
    state.clockTimer = setInterval(tick, 1000);
  }
  function stopClock() { clearInterval(state.clockTimer); state.clockTimer = null; }

  // ---------- результат / 3D-вьювер ----------
  function viewerFail(message) {
    el.loader.hidden = true;
    el.viewerError.textContent = message;
    el.viewerError.hidden = false;
  }

  async function showResult(modelUrl) {
    el.viewerError.hidden = true;
    el.loader.hidden = false;
    el.loaderBar.style.width = '0%';
    el.exposure.value = '1';
    el.viewer.exposure = 1;
    el.viewer.autoRotate = true;
    el.btnRotate.setAttribute('aria-pressed', 'true');

    el.download.href = `${modelUrl}?download=true`;
    el.download.download = `voxelize-${state.taskId.slice(0, 8)}.glb`;
    el.resultMeta.textContent = state.elapsedAtFinish ? `готово за ${state.elapsedAtFinish}` : `task ${state.taskId.slice(0, 8)}`;
    el.btnFullscreen.hidden = !document.fullscreenEnabled;
    setView('result');

    // если vendor/model-viewer.min.js не загрузился — не висим вечно на «загрузке»
    const defined = await Promise.race([
      customElements.whenDefined('model-viewer').then(() => true),
      delay(6000).then(() => false),
    ]);
    if (!defined) { viewerFail('Не загрузился просмотрщик (vendor/model-viewer.min.js). Модель можно скачать кнопкой ниже.'); return; }
    el.viewer.src = modelUrl;
  }

  el.viewer.addEventListener('progress', (e) => {
    el.loaderBar.style.width = `${Math.round((e.detail?.totalProgress ?? 0) * 100)}%`;
  });
  el.viewer.addEventListener('load', () => { el.loader.hidden = true; });
  el.viewer.addEventListener('error', () => viewerFail('Не удалось загрузить или отобразить модель. Попробуйте скачать файл.'));

  el.btnRotate.addEventListener('click', () => {
    const on = !el.viewer.autoRotate;
    el.viewer.autoRotate = on;
    el.btnRotate.setAttribute('aria-pressed', String(on));
  });
  el.btnReset.addEventListener('click', () => {
    el.viewer.cameraOrbit = 'auto auto auto';
    el.viewer.cameraTarget = 'auto auto auto';
    el.viewer.fieldOfView = 'auto';
    el.viewer.jumpCameraToGoal?.();
  });
  el.exposure.addEventListener('input', () => { el.viewer.exposure = Number(el.exposure.value); });
  el.btnFullscreen.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else el.stage.requestFullscreen?.();
  });
  el.btnShot.addEventListener('click', async () => {
    if (typeof el.viewer.toBlob !== 'function') return;
    try {
      const blob = await el.viewer.toBlob({ mimeType: 'image/png', idealAspect: false });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'voxelize-preview.png'; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch { /* модель ещё не загружена — молча игнорируем */ }
  });

  function resetAll() {
    stopPolling();
    stopClock();
    store.clear();
    state.taskId = null;
    state.elapsedAtFinish = '';
    el.viewer.removeAttribute('src');
    clearFiles();
    showNotice([]);
    setView('upload');
  }
  el.btnNew.addEventListener('click', resetAll);
  el.btnRetry.addEventListener('click', resetAll);

  // ---------- drag-and-drop ----------
  const openPicker = () => el.input.click();
  el.dropzone.addEventListener('click', openPicker);
  el.dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPicker(); }
  });
  el.input.addEventListener('change', () => { addFiles([...el.input.files]); el.input.value = ''; });

  ['dragenter', 'dragover'].forEach((type) => el.dropzone.addEventListener(type, (e) => {
    e.preventDefault();
    el.dropzone.classList.add('dragover');
  }));
  el.dropzone.addEventListener('dragleave', (e) => {
    if (!el.dropzone.contains(e.relatedTarget)) el.dropzone.classList.remove('dragover');
  });
  el.dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    el.dropzone.classList.remove('dragover');
    addFiles([...(e.dataTransfer?.files ?? [])]);
  });
  // промах мимо зоны не должен открывать картинку в браузере и уводить со страницы
  ['dragover', 'drop'].forEach((type) => window.addEventListener(type, (e) => e.preventDefault()));

  document.addEventListener('paste', (e) => {          // Ctrl+V со скриншотом / копией картинки
    if (document.body.dataset.view !== 'upload') return;
    const images = [...(e.clipboardData?.files ?? [])].filter((f) => f.type.startsWith('image/'));
    if (images.length) { e.preventDefault(); addFiles(images); }
  });

  el.btnStart.addEventListener('click', submit);
  el.btnClear.addEventListener('click', () => { showNotice([]); clearFiles(); });

  window.addEventListener('online', pingServer);
  window.addEventListener('offline', () => setSys('down'));
  window.addEventListener('beforeunload', () => state.files.forEach((f) => URL.revokeObjectURL(f.url)));

  // ---------- восстановление задачи после перезагрузки страницы ----------
  async function restore() {
    const saved = store.read();
    if (!saved?.id) return;
    try {
      const res = await fetchT(`${API}/status/${encodeURIComponent(saved.id)}`, { cache: 'no-store' });
      if (!res.ok) { store.clear(); return; }
      const s = await res.json();
      if (s.status === 'FAILED') { store.clear(); return; }
      state.taskId = saved.id;
      if (s.status === 'DONE') {
        state.elapsedAtFinish = '';
        showResult(`${API}/model/${saved.id}.glb`);
      } else {
        beginTask(saved.id, Number(saved.startedAt) || Date.now());
        logLine('Сессия восстановлена: продолжаю следить за задачей', 'warn');
      }
    } catch { /* сервер недоступен — остаёмся на экране загрузки, запись сохраняем */ }
  }

  // ---------- старт ----------
  setView('upload', { scroll: false });
  renderFiles();
  pingServer();
  restore();
})();
