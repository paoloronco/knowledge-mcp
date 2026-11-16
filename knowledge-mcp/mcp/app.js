const $ = id => document.getElementById(id);
let current = null, policy = null, wizardStep = 0, authRequired = false, folderPaths = [], eligibleFiles = [];
let messageTimer = null, refreshError = null, refreshing = false;
let modelRenderKey = '';
const setupLabels = ['Dashboard login', 'Service health', 'Document folders', 'Indexing policy', 'Eligible documents', 'Dry-run test', 'Initial indexing'];
const setupPaths = ['/setup/login', '/setup/health', '/setup/folders', '/setup/policy', '/setup/eligible', '/setup/dry-run', '/setup/indexing'];
const dashboardPages = ['overview', 'indexing', 'folders', 'policy', 'settings'];

let pendingActions = 0;
function renderBusy() {
  const el = $('global-busy');
  if (!el) return;
  el.classList.toggle('hidden', pendingActions === 0);
  el.setAttribute('aria-busy', pendingActions ? 'true' : 'false');
}
async function api(path, data) {
  const post = data !== undefined;
  if (post) { pendingActions++; renderBusy(); }
  try {
    const options = post ? {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Control-Token': token}, body: JSON.stringify(data)
    } : {};
    const response = await fetch(path, options), body = await response.json();
    if (!response.ok) throw Error(body.error || 'Request failed');
    return body;
  } finally {
    if (post) { pendingActions--; renderBusy(); }
  }
}
async function downloadBackup() {
  $('backup-download').disabled = true;
  $('backup-status').textContent = 'Creating backup. Search is temporarily paused…';
  try {
    const result = await api('/api/backup', {}), link = document.createElement('a');
    link.href = result.url; link.download = 'knowledge-mcp-backup.tar.gz';
    link.click();
    $('backup-status').textContent = 'Backup ready. Download started; search has resumed.';
  } catch (e) { $('backup-status').textContent = e.message; }
  finally { $('backup-download').disabled = false; }
}
async function restoreBackup() {
  const file = $('backup-file').files[0];
  if (!file) { $('backup-status').textContent = 'Select a backup archive first.'; return; }
  if (!confirm('Replace all settings, documents and indexes on this instance with the backup? The app will restart and use the saved dashboard password.')) return;
  $('backup-restore').disabled = true;
  $('backup-status').textContent = 'Uploading and validating backup…';
  try {
    const response = await fetch('/api/restore', {method: 'POST', headers: {'Content-Type': 'application/gzip', 'X-Control-Token': token}, body: file});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Restore failed');
    $('backup-status').textContent = 'Backup restored. The app is restarting; reload this page shortly and use the saved password.';
  } catch (e) { $('backup-status').textContent = e.message; }
  finally { $('backup-restore').disabled = false; }
}
function message(value, error = false) {
  clearTimeout(messageTimer);
  const notice = $('message');
  notice.textContent = value;
  notice.classList.toggle('error', error);
  notice.classList.toggle('visible', Boolean(value));
  messageTimer = setTimeout(() => {
    notice.classList.remove('visible');
    notice.textContent = '';
  }, error ? 7000 : 4000);
}
function badge(name, good, pending) {
  const el = document.createElement('span'); el.className = 'badge' + (good ? ' ok' : pending ? ' pending' : '');
  el.textContent = name + ': ' + (good ? 'ready' : pending || 'unavailable'); return el;
}
function renderSetupProgress(status) {
  const done = Math.max(0, Math.min(7, status.config.setup_step || 0));
  $('setup-progress-bar').value = done;
  $('wizard-progress').textContent = done === 7 ? 'Setup complete' : status.config.onboarding_complete ? status.index_running ? 'Initial indexing in progress · 6 of 7 steps complete' : 'Waiting for initial indexing · 6 of 7 steps complete' : `Step ${wizardStep + 1} of 7 · ${setupLabels[wizardStep]}`;
  for (const item of $('setup-steps').children) {
    const index = Number(item.dataset.progressStep);
    item.classList.toggle('complete', index < done);
    item.classList.toggle('current', index === wizardStep);
    item.classList.toggle('failed', index === 6 && done === 6 && status.config.onboarding_complete && !status.index_running && status.last_result && !status.last_result.dry_run && status.last_result.exit_code !== 0);
    if (index === wizardStep) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  }
  if (done === 6 && status.config.onboarding_complete && !status.index_running && status.last_result && !status.last_result.dry_run && status.last_result.exit_code !== 0) $('wizard-progress').textContent = status.index_error_count ? 'Initial indexing finished with document errors · review and retry' : 'Initial indexing failed · review the log and retry';
}
function showStep(step, push = true) {
  wizardStep = Math.max(0, Math.min(6, step));
  if (push && location.pathname !== setupPaths[wizardStep]) history.pushState({}, '', setupPaths[wizardStep]);
  document.querySelectorAll('.step').forEach(el => el.classList.toggle('active', Number(el.dataset.step) === wizardStep));
  if (current) renderSetupProgress(current);
  const list = $('setup-steps'), item = list.children[wizardStep];
  if (item) list.scrollTo({left: item.offsetLeft - list.offsetLeft - (list.clientWidth - item.clientWidth) / 2, behavior: 'smooth'});
}
function backStep() { showStep(wizardStep - 1); }
async function nextStep() {
  try {
    const step = Math.min(6, wizardStep + 1);
    const result = await api('/api/onboarding/progress', {step});
    current.config.setup_step = result.setup_step;
    showStep(step);
  } catch (e) { message(e.message, true); }
}
function sourceModeChanged() {
  const mode = document.activeElement?.id?.endsWith('source-mode') ? document.activeElement.value : current.config.source_selection;
  $('setup-source-mode').value = mode; $('dashboard-source-mode').value = mode;
}
function fillSourceInputs() {
  folderPaths = current.document_paths || (current.config.folders || []).map(folder => {
    const root = current.config.source_mode === 'host_agent' ? current.config.host_root : current.config.source_root;
    return root ? root.replace(/\/$/, '') + (folder ? '/' + folder : '') : '';
  }).filter(Boolean);
  $('setup-source-root').value = ''; $('dashboard-source-root').value = '';
  $('setup-source-mode').value = current.config.source_selection; $('dashboard-source-mode').value = current.config.source_selection;
  sourceModeChanged();
  renderFolderLists();
}
function renderFolderLists() {
  for (const id of ['setup-folder-list', 'dashboard-folder-list']) {
    const list = $(id); list.replaceChildren();
    for (const path of folderPaths) {
      const item = document.createElement('li'), name = document.createElement('span'), remove = document.createElement('button');
      name.textContent = path; remove.textContent = 'Remove'; remove.className = 'secondary'; remove.setAttribute('aria-label', 'Remove ' + path);
      remove.onclick = () => removeFolder(path); item.append(name, remove); list.append(item);
    }
  }
  $('folders-next').disabled = folderPaths.length === 0;
}
async function addFolder(fromWizard) {
  const input = $(fromWizard ? 'setup-source-root' : 'dashboard-source-root');
  const note = $(fromWizard ? 'setup-folder-check' : 'dashboard-folder-check');
  const button = $(fromWizard ? 'setup-add-folder' : 'dashboard-add-folder');
  const path = input.value.trim(), selection = $(fromWizard ? 'setup-source-mode' : 'dashboard-source-mode').value;
  if (!path || folderPaths.includes(path)) { note.textContent = path ? 'This folder is already in the list.' : 'Enter a folder path.'; return; }
  button.disabled = true; note.textContent = 'Checking folder access…';
  try {
    await api('/api/folder/check', {path, source_selection: selection});
    const result = await api('/api/config', {document_paths: [...folderPaths, path], source_selection: selection, interval_hours: Number($('interval').value || 0)});
    current.config = result; folderPaths = [...folderPaths, path]; input.value = '';
    note.textContent = 'Folder reachable and added.'; renderFolderLists(); await refresh();
  } catch (e) { note.textContent = e.message; message(e.message, true); }
  finally { button.disabled = false; }
}
async function removeFolder(path) {
  try {
    const remaining = folderPaths.filter(item => item !== path);
    const result = await api('/api/config', {document_paths: remaining, source_selection: $('setup-source-mode').value, interval_hours: Number($('interval').value || 0)});
    current.config = result; folderPaths = remaining; renderFolderLists(); await refresh();
    message('Folder removed.');
  } catch (e) { message(e.message, true); }
}
async function advanceFolders() {
  if (!folderPaths.length) { message('Add at least one reachable folder.', true); return; }
  await nextStep();
}
async function saveSchedule() {
  try { await api('/api/config', {interval_hours: Number($('interval').value)}); message('Schedule saved.'); await refresh(); }
  catch (e) { message(e.message, true); }
}
function renderScanStatus(s) {
  const progress = $('setup-sync-progress');
  const phase = s.agent_progress?.phase;
  let text;
  if (s.scan_error) text = 'Scan failed: ' + s.scan_error;
  else if (s.scan_complete) text = `Scan complete: ${s.scan_checked} ${s.scan_checked === 1 ? 'file' : 'files'} checked, ${s.scan_eligible_count} eligible ${s.scan_eligible_count === 1 ? 'document' : 'documents'}.` + (s.scan_eligible_count ? '' : ' Review the folder and indexing policy, then scan again.');
  else if (s.config.source_mode === 'host_agent' && !s.agent_connected) text = s.agent_managed ? 'Waiting for the host agent. Check its Docker service.' : 'Host folder access is not configured.';
  else if (phase === 'scanning' || s.scan_running && s.config.source_mode !== 'host_agent') text = `Scanning ${s.config.source_mode === 'host_agent' ? 'host folder' : 'document folder'}: ${s.scan_checked} files checked, ${s.scan_eligible_count} eligible…`;
  else if (phase === 'planning') text = 'Comparing eligible documents with the synchronized copy…';
  else if (phase === 'transferring') text = `Transferring changed documents: ${s.agent_progress.completed} of ${s.agent_progress.total}…`;
  else if (phase === 'finalizing') text = 'Finalizing host folder sync…';
  else text = s.scan_running ? 'Waiting for the host agent to start the scan…' : 'Press Scan to check eligible documents.';
  $('setup-scan-status').textContent = text;
  const showProgress = !s.scan_complete && s.scan_running;
  progress.classList.toggle('hidden', !showProgress);
  if (showProgress && s.agent_progress?.phase === 'transferring' && s.agent_progress.total > 0) progress.value = 100 * s.agent_progress.completed / s.agent_progress.total;
  else progress.removeAttribute('value');
  const list = $('setup-eligible-list'); list.replaceChildren();
  if (s.scan_complete) for (const name of s.scan_eligible_preview || []) { const item = document.createElement('li'); item.textContent = name; list.append(item); }
  $('eligible-view-all').classList.toggle('hidden', !s.scan_complete || !s.scan_eligible_count);
  $('scan-next').disabled = !s.scan_ready;
  $('scan-again').disabled = s.scan_running;
  $('scan-again').textContent = s.scan_complete ? 'Scan again' : 'Scan';
}
async function startScan() { try { if ($('eligible-dialog').open) $('eligible-dialog').close(); eligibleFiles = []; await api('/api/scan', {}); await refresh(); } catch (e) { message(e.message, true); } }
async function openEligibleFiles() {
  try {
    eligibleFiles = (await api('/api/scan/files')).files;
    $('eligible-total').textContent = eligibleFiles.length + (eligibleFiles.length === 1 ? ' eligible document' : ' eligible documents');
    $('eligible-all').textContent = eligibleFiles.join('\n');
    $('eligible-dialog').showModal();
  } catch (e) { message(e.message, true); }
}
function downloadEligible(format) {
  const data = format === 'json' ? JSON.stringify(eligibleFiles, null, 2) + '\n' : eligibleFiles.join('\n') + '\n';
  const link = document.createElement('a'), url = URL.createObjectURL(new Blob([data], {type: format === 'json' ? 'application/json' : 'text/plain'}));
  link.href = url; link.download = 'eligible-documents.' + format; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function syncNow() {
  try { await api('/api/agent/refresh', {}); message('Host sync requested. The agent will scan the folder shortly.'); }
  catch (e) { message(e.message, true); }
}

async function login() {
  try {
    const response = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: $('login-password').value})});
    const body = await response.json(); if (!response.ok) throw Error(body.error || 'Sign-in failed'); location.reload();
  } catch (e) { message(e.message, true); }
}
async function logout() { try { await api('/api/logout', {}); location.reload(); } catch (e) { message(e.message, true); } }
function toggleSetupPassword() {
  const enabled = $('login-enabled').checked;
  $('login-password-fields').classList.toggle('hidden', !enabled);
  $('setup-login-next').textContent = enabled ? 'Continue with login' : 'Continue without login';
  $('setup-password-note').textContent = authRequired ? 'Leave empty to keep the current password.' : '';
}
async function setupSecurity() {
  try {
    if ($('login-enabled').checked) {
      const password = $('setup-password').value;
      if (authRequired && !password) { await nextStep(); return; }
      await api('/api/security', {enabled: true, password});
    } else {
      await api('/api/security', {enabled: false});
    }
    history.replaceState({}, '', setupPaths[1]);
    location.reload();
  } catch (e) { message(e.message, true); }
}
async function setPassword() { try { await api('/api/security', {enabled: true, password: $('new-password').value}); location.reload(); } catch (e) { message(e.message, true); } }
async function disablePassword() { try { await api('/api/security', {enabled: false}); if (!current.config.onboarding_complete) history.replaceState({}, '', '/'); location.reload(); } catch (e) { message(e.message, true); } }

const extensions = ['.pdf', '.docx', '.pptx', '.md', '.txt', '.html', '.htm'];
function policyWidget(id) {
  const node = $(id); node.replaceChildren();
  const title = document.createElement('h3'); title.textContent = 'Included extensions'; node.append(title);
  const checks = document.createElement('div'); checks.className = 'extensions';
  for (const ext of extensions) {
    const label = document.createElement('label'), box = document.createElement('input');
    box.type = 'checkbox'; box.dataset.extension = ext; box.checked = policy.include_extensions.includes(ext);
    label.append(box, document.createTextNode(' ' + ext)); checks.append(label);
  }
  node.append(checks);
  for (const [key, caption] of [
    ['exclude_directories', 'Excluded directories (one per line)'], ['exclude_top_level', 'Excluded top-level entries'],
    ['exclude_files', 'Excluded files (name or relative path)'], ['exclude_extensions', 'Excluded extensions']
  ]) {
    const label = document.createElement('label'); label.textContent = caption;
    const input = document.createElement('textarea'); input.dataset.policyKey = key; input.value = (policy[key] || []).join('\n'); node.append(label, input);
  }
  const label = document.createElement('label'); label.textContent = 'Maximum file size (MB)';
  const size = document.createElement('input'); size.type = 'number'; size.min = '1'; size.dataset.policySize = 'true'; size.value = policy.max_file_size_mb; node.append(label, size);
}
async function loadPolicy() {
  const result = await api('/api/policy'); policy = result.policy; $('policy-yaml').value = result.content;
  policyWidget('wizard-policy'); policyWidget('dashboard-policy');
}
function formPolicy(id) {
  const node = $(id), values = {...policy};
  values.include_extensions = [...node.querySelectorAll('[data-extension]:checked')].map(x => x.dataset.extension);
  for (const input of node.querySelectorAll('[data-policy-key]')) values[input.dataset.policyKey] = input.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  values.max_file_size_mb = Number(node.querySelector('[data-policy-size]').value); return values;
}
async function savePolicy(advance) {
  try { await api('/api/policy', {policy: formPolicy(advance ? 'wizard-policy' : 'dashboard-policy')}); await loadPolicy(); message('Policy saved.'); if (advance) await nextStep(); }
  catch (e) { message(e.message, true); }
}
async function saveYaml() { try { await api('/api/policy', {content: $('policy-yaml').value}); await loadPolicy(); message('YAML saved.'); } catch (e) { message(e.message, true); } }
async function service(name, action) { try { await api('/api/service', {name, action}); message(`${name}: ${action} requested.`); await refresh(); } catch (e) { message(e.message, true); } }
async function runIndex(dry_run, rebuild = false) {
  if (rebuild && !confirm('Rebuild the selected model’s index from scratch? Existing vectors for this model will be replaced and search will pause during indexing. Documents and other models’ indexes are kept.')) return;
  try { await api('/api/index', {dry_run, rebuild}); message(dry_run ? 'Dry run started.' : rebuild ? 'Full rebuild started.' : 'Indexing started.'); if (dry_run) $('dry-result').textContent = 'Test running…'; await refresh(); }
  catch (e) { message(e.message, true); }
}
function appendIndexError(list, error, running = false) {
  const item = document.createElement('li'), source = document.createElement('code'), detail = document.createElement('span');
  source.textContent = error.source || 'Unknown document';
  detail.textContent = `${error.stage || 'indexing'} · ${error.error_type || 'Error'}: ${error.message || 'No details recorded'}`;
  item.append(source, detail);
  if (error.ignored || error.auto_excluded) {
    const state = document.createElement('span'); state.textContent = error.auto_excluded ? ' · Automatically excluded in this version; retry indexing' : ' · Ignored in indexing policy'; item.append(state);
  } else if (error.can_ignore) {
    const button = document.createElement('button'); button.className = 'secondary'; button.textContent = 'Ignore this file';
    button.disabled = running;
    button.onclick = () => ignoreIndexError(error.source); item.append(button);
  }
  list.append(item);
}
async function ignoreIndexError(source) {
  try {
    const result = await api('/api/policy'), next = result.policy;
    next.exclude_files = [...new Set([...(next.exclude_files || []), source])];
    await api('/api/policy', {policy: next});
    await loadPolicy(); await refresh();
    message('File excluded. Wait for the folder to sync, then run the incremental update.');
  } catch (e) { message(e.message, true); }
}
async function openIndexErrors() {
  try {
    const result = await api('/api/index/errors'), list = $('index-errors-all-list');
    list.replaceChildren();
    for (const error of result.errors) appendIndexError(list, error, current?.index_running);
    $('index-errors-dialog').showModal();
  } catch (e) { message(e.message, true); }
}
function renderIndexErrors(s) {
  const count = s.index_error_count || 0;
  const failed = !s.index_running && s.last_result && !s.last_result.dry_run && s.last_result.exit_code !== 0;
  $('index-errors-card').classList.toggle('hidden', !count && !failed);
  if (count) {
    const historical = s.index_error_origin === 'legacy' ? ' These entries come from an older error log and may include earlier attempts.' : '';
    $('index-errors-summary').textContent = `${count} ${count === 1 ? 'document has' : 'documents have'} errors. Successfully indexed documents remain saved.${historical}`;
  } else if (failed) $('index-errors-summary').textContent = 'Indexing stopped before a document error was recorded. Download the full log for details.';
  else $('index-errors-summary').textContent = '';
  const list = $('index-errors-list'); list.replaceChildren();
  for (const error of s.index_errors || []) appendIndexError(list, error, s.index_running);
  $('index-errors-all').classList.toggle('hidden', count <= (s.index_errors || []).length);
  for (const id of ['download-index-full', 'setup-download-full']) $(id).classList.toggle('hidden', !s.index_log_available);
  for (const id of ['download-index-stages', 'setup-download-stages']) $(id).classList.toggle('hidden', !s.index_stages_available);
  $('index-stages-note').classList.toggle('hidden', !s.index_log_available || s.index_stages_available);
}
async function selectEmbeddingModel(model) {
  try {
    await api('/api/embedding-model', {model});
    message('Model selected. Run indexing to make it searchable.');
    await refresh();
  } catch (e) { message(e.message, true); }
}
function renderEmbeddingModels(s) {
  const selected = s.config.embedding_model, active = s.config.active_embedding_model;
  const key = `${selected}|${active}|${s.index_running}`;
  if (key === modelRenderKey) return;
  modelRenderKey = key;
  for (const target of ['setup-model-options', 'dashboard-model-options']) {
    const cards = Object.entries(s.embedding_models || {}).map(([id, spec]) => {
      const card = document.createElement('div'); card.className = 'model-card';
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'model-option' + (selected === id ? ' selected' : '');
      button.setAttribute('aria-pressed', String(selected === id));
      button.disabled = s.index_running;
      button.onclick = () => selectEmbeddingModel(id);
      const header = document.createElement('div'); header.className = 'model-option-header';
      const title = document.createElement('strong');
      title.textContent = spec.name;
      const tags = document.createElement('div'); tags.className = 'model-option-tags';
      if (id === 'e5-small') {
        const recommended = document.createElement('span'); recommended.className = 'model-tag recommended';
        recommended.textContent = 'Recommended'; tags.append(recommended);
      }
      if (selected === id) {
        const chosen = document.createElement('span'); chosen.className = 'model-tag chosen';
        chosen.textContent = 'Selected'; tags.append(chosen);
      }
      header.append(title, tags);
      const useCase = document.createElement('p'); useCase.className = 'model-use-case'; useCase.textContent = spec.use_case;
      const metrics = document.createElement('div'); metrics.className = 'model-metrics';
      for (const [icon, label, value] of [
        ['⚡', 'CPU speed', spec.speed], ['▥', 'Memory', spec.resources],
        ['◈', 'Vector size', `${spec.dimensions} dims`], ['▣', 'Runs on', 'CPU / GPU']
      ]) {
        const metric = document.createElement('div'); metric.className = 'model-metric';
        const symbol = document.createElement('span'); symbol.className = 'model-metric-icon'; symbol.textContent = icon; symbol.setAttribute('aria-hidden', 'true');
        const copy = document.createElement('span'); copy.className = 'model-metric-copy';
        const caption = document.createElement('span'); caption.className = 'model-metric-label'; caption.textContent = label;
        const amount = document.createElement('strong'); amount.className = 'model-metric-value'; amount.textContent = value;
        copy.append(caption, amount); metric.append(symbol, copy); metrics.append(metric);
      }
      const note = document.createElement('p'); note.className = 'model-option-note'; note.textContent = spec.note;
      button.append(header, useCase, metrics, note);
      const source = document.createElement('a');
      source.href = spec.url; source.target = '_blank'; source.rel = 'noopener noreferrer';
      source.textContent = 'Official model card';
      card.append(button, source);
      return card;
    });
    $(target).replaceChildren(...cards);
  }
  const name = id => s.embedding_models?.[id]?.name || (id === 'environment' ? 'Custom environment model' : 'None');
  const state = active && active !== selected
    ? `Search currently uses ${name(active)}. ${name(selected)} has a separate index and becomes searchable after a successful indexing run. Both indexes use disk space.`
    : active ? `Search uses ${name(active)}. Changing models keeps this index until the new one is ready.`
      : `Selected: ${name(selected)}. Search becomes available after indexing completes.`;
  $('setup-model-state').textContent = state;
  $('dashboard-model-state').textContent = state;
}
async function startInitialIndex() {
  try {
    await api('/api/index', {dry_run: false}); await api('/api/onboarding', {});
    goDashboardPage('indexing', true);
    message('Indexing started. Progress is shown here.'); await refresh();
  } catch (e) { message(e.message, true); }
}
async function skipInitialIndex() {
  try {
    await api('/api/onboarding/skip-index', {});
    goDashboardPage('overview', true);
    message('Initial indexing postponed. Start it later from Indexing.');
    await refresh();
  } catch (e) { message(e.message, true); }
}
function dashboardPath(page) { return page === 'overview' ? '/dashboard' : '/dashboard/' + page; }
function goIndexPage(page, push = false) {
  const chosen = page === 'model' ? 'model' : 'run';
  const path = chosen === 'model' ? '/dashboard/indexing/model' : '/dashboard/indexing';
  if (push && location.pathname !== path) history.pushState({}, '', path);
  for (const panel of document.querySelectorAll('.index-pane')) panel.classList.toggle('hidden', panel.dataset.indexPage !== chosen);
  for (const link of document.querySelectorAll('[data-index-nav]')) {
    const active = link.dataset.indexNav === chosen;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
}
function goDashboardPage(page, push = false) {
  const chosen = dashboardPages.includes(page) ? page : 'overview';
  if (push && location.pathname !== dashboardPath(chosen)) history.pushState({}, '', dashboardPath(chosen));
  for (const panel of document.querySelectorAll('.dashboard-panel')) panel.classList.toggle('active', panel.dataset.page === chosen);
  for (const link of document.querySelectorAll('[data-nav-page]')) link.classList.toggle('active', link.dataset.navPage === chosen);
  if (chosen === 'indexing') goIndexPage(location.pathname === '/dashboard/indexing/model' ? 'model' : 'run');
}
function syncRoute(status) {
  if (!status.config.onboarding_complete && location.pathname !== '/dashboard/settings') {
    const available = Math.max(0, Math.min(6, status.config.setup_step || 0));
    const requested = setupPaths.indexOf(location.pathname);
    const step = requested < 0 ? available : Math.min(requested, available);
    if (location.pathname !== setupPaths[step]) history.replaceState({}, '', setupPaths[step]);
    showStep(step, false);
    return;
  }
  const requested = location.pathname === '/dashboard' ? 'overview' : location.pathname === '/dashboard/indexing/model' ? 'indexing' : location.pathname.startsWith('/dashboard/') ? location.pathname.slice('/dashboard/'.length) : '';
  const page = dashboardPages.includes(requested) ? requested : 'overview';
  if (location.pathname !== dashboardPath(page) && location.pathname !== '/dashboard/indexing/model') history.replaceState({}, '', dashboardPath(page));
  goDashboardPage(page);
}
function renderIndexProgress(s) {
  const progress = s.config.initial_index_skipped && !s.index_running && s.last_result?.dry_run ? null : s.index_progress;
  const bar = $('index-progress'), overview = $('overview-indexing-progress'), overviewBar = $('overview-index-progress');
  overview.classList.toggle('hidden', !s.index_running);
  if (!progress) {
    bar.classList.add('hidden'); $('index-progress-label').textContent = s.index_running ? 'Starting…' : '';
    $('overview-index-progress-label').textContent = s.index_running ? 'Starting…' : '';
    if (s.index_running) overviewBar.removeAttribute('value');
    return;
  }
  const stages = {discovering: 'Discovering eligible documents', hashing: 'Calculating file hashes', dry_run: 'Testing document parsing', loading_model: 'Loading the embedding model', indexing: 'Indexing documents', complete: 'Completed', failed: 'Finished with errors'};
  const endedWithError = !s.index_running && s.last_result && s.last_result.exit_code !== 0;
  const stage = endedWithError && progress.stage !== 'failed' ? `${s.index_error_count ? 'Finished with errors' : 'Stopped'} during ${stages[progress.stage] || progress.stage}` : stages[progress.stage] || progress.stage;
  const total = progress.total, count = progress.completed || 0;
  const percent = total > 0 ? Math.min(100, Math.round(count * 100 / total)) : null;
  const completed = !s.index_running && s.last_result?.exit_code === 0 && progress.stage === 'complete';
  const label = completed ? `Completed: ${count}${percent === null ? '' : ` (${percent}%)`}` : percent === null ? stage + '…' : `${stage}: ${count} of ${total} (${percent}%)`;
  $('index-progress-label').textContent = label;
  bar.classList.toggle('hidden', !s.index_running || progress.stage === 'complete');
  bar.classList.toggle('failed', progress.stage === 'failed' || endedWithError);
  if (percent === null) bar.removeAttribute('value'); else bar.value = percent;
  if (s.index_running) {
    $('overview-index-progress-label').textContent = label;
    if (percent === null) overviewBar.removeAttribute('value'); else overviewBar.value = percent;
  }
}
function renderGpuStatus(s) {
  const gpu = s.gpu || {checking: true};
  const devices = gpu.devices || [];
  const preference = s.embedding_device_preference || 'auto';
  let tone = 'cpu', title = 'CPU embeddings', badge = 'Active', description = '';
  if (gpu.checking) {
    tone = 'checking'; title = 'Checking hardware'; badge = 'Checking';
  } else if (preference === 'cuda' && !gpu.usable) {
    tone = 'warning'; title = 'GPU unavailable'; badge = 'Action needed';
    description = gpu.reason || 'CUDA was requested, but GPU acceleration is unavailable.';
  } else if (gpu.usable && preference !== 'cpu') {
    tone = 'ready'; title = 'GPU embeddings'; badge = 'Active';
  } else if (preference === 'cpu') {
    badge = 'Selected';
    description = devices.length ? 'GPU detected; CPU selected in settings.' : '';
  } else if (devices.length) {
    badge = 'GPU unavailable';
    description = gpu.reason || 'GPU acceleration is unavailable.';
  } else {
    description = 'No NVIDIA GPU available in this container.';
  }
  const details = devices.map(item => {
    const parts = [item.name];
    if (item.driver) parts.push(`Driver ${item.driver}`);
    if (item.memory_total_mb != null) parts.push(`${item.memory_free_mb} / ${item.memory_total_mb} MiB free`);
    return parts.join(' · ');
  });
  if (gpu.cuda_runtime) details.push(`CUDA ${gpu.cuda_runtime}`);
  for (const id of ['wizard-gpu-status', 'overview-gpu-status', 'index-gpu-status']) {
    const card = $(id);
    card.className = `gpu-status gpu-status-${tone}`;
    const icon = document.createElement('span');
    icon.className = 'gpu-status-icon';
    icon.textContent = '▣';
    icon.setAttribute('aria-hidden', 'true');
    const body = document.createElement('div');
    body.className = 'gpu-status-body';
    const heading = document.createElement('div');
    heading.className = 'gpu-status-heading';
    const titleNode = document.createElement('strong');
    titleNode.textContent = title;
    const badgeNode = document.createElement('span');
    badgeNode.className = 'gpu-status-badge';
    badgeNode.textContent = badge;
    heading.append(titleNode, badgeNode);
    body.append(heading);
    if (description) {
      const summary = document.createElement('p');
      summary.className = 'gpu-status-description';
      summary.textContent = description;
      body.append(summary);
    }
    if (details.length) {
      const specs = document.createElement('div');
      specs.className = 'gpu-status-details';
      for (const detail of details) {
        const item = document.createElement('span');
        item.textContent = detail;
        specs.append(item);
      }
      body.append(specs);
    }
    card.replaceChildren(icon, body);
  }
}
function syncInProgress(s) {
  return s.config.source_mode === 'host_agent' && !s.source_ready && !s.agent_error && s.agent_connected && (s.agent_syncing || s.scan_running || Boolean(s.agent_progress));
}
function syncProgressDetail(s) {
  const progress = s.agent_progress;
  if (!progress) return '';
  if (progress.phase === 'scanning') return `: ${progress.checked || 0} files checked, ${progress.eligible || 0} eligible`;
  if (progress.phase === 'transferring' && progress.total) return `: ${progress.completed || 0} of ${progress.total} documents transferred`;
  if (progress.phase === 'planning') return ': planning transfer';
  if (progress.phase === 'finalizing') return ': finalizing';
  return '';
}
function renderReadiness(s) {
  const banner = $('service-banner'), r = s.readiness;
  banner.classList.toggle('hidden', !r);
  if (!r) return;
  for (const tone of ['ready', 'busy', 'warning']) banner.classList.toggle('service-banner-' + tone, r.tone === tone);
  banner.classList.toggle('spinning', Boolean(r.spinner));
  $('service-banner-mark').textContent = r.tone === 'ready' ? 'OK' : r.tone === 'warning' ? '!' : '…';
  $('service-banner-title').textContent = r.title || '';
  $('service-banner-detail').textContent = r.detail || '';
  const labels = {done: 'ready', active: 'in progress', pending: 'waiting', error: 'error'};
  $('service-banner-steps').textContent = (r.steps || []).map(x => x.label + ': ' + (labels[x.state] || x.state)).join('  ·  ');
  const action = r.action, button = $('service-banner-action');
  button.classList.toggle('hidden', !action);
  if (action) { button.textContent = action.label; button.onclick = () => readinessAction(action.kind); }
}
function readinessAction(kind) {
  if (kind === 'start_qdrant' || kind === 'start_mcp') service(kind === 'start_qdrant' ? 'qdrant' : 'mcp', 'start');
  else if (kind === 'open_indexing') goDashboardPage('indexing', true);
  else if (kind === 'open_folders') goDashboardPage('folders', true);
}
function renderConnection(s) {
  const port = s.mcp_port || 8000;
  const host = (typeof location !== 'undefined' && location.hostname) || 'HOST_IP';
  const origin = (typeof location !== 'undefined' && location.origin) || ('http://' + host + ':8080');
  $('mcp-endpoint').value = 'http://' + host + ':' + port + '/mcp';
  $('dashboard-origin').textContent = origin;
  $('app-version').textContent = s.version || '—';
  $('update-command').value = 'bash scripts/self-update.sh';
}
function copyText(id, label) {
  const el = $(id), text = el.value || '';
  const done = () => { $('connection-status').textContent = label + ' copied to clipboard.'; };
  if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(el, done));
  } else fallbackCopy(el, done);
}
function fallbackCopy(el, done) {
  try {
    el.removeAttribute('readonly'); el.select();
    if (document.execCommand && document.execCommand('copy')) done();
    else $('connection-status').textContent = 'Select the text above and copy it manually.';
    el.setAttribute('readonly', 'readonly');
  } catch (e) { $('connection-status').textContent = 'Select the text above and copy it manually.'; }
}
function copyEndpoint() { copyText('mcp-endpoint', 'Endpoint'); }
function copyUpdate() { copyText('update-command', 'Update command'); }
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const s = await api('/api/status'); current = s;
    renderConnection(s);
    renderReadiness(s);
    renderEmbeddingModels(s);
    syncRoute(s);
    renderSetupProgress(s);
    renderScanStatus(s);
    renderIndexProgress(s);
    renderIndexErrors(s);
    renderGpuStatus(s);
    const syncing = syncInProgress(s);
    const syncDetailText = syncing ? syncProgressDetail(s) : '';
    const documentsPending = syncing ? 'syncing…' : !s.source_ready && !s.agent_error && s.agent_connected && !s.agent_synced && s.config.source_mode === 'host_agent' ? 'waiting for sync' : null;
    const healthBadges = [badge('App', s.app_ready), badge('Qdrant', s.qdrant_ready), badge('MCP', s.mcp_running), badge('Documents', s.source_ready, documentsPending), ...(s.config.source_mode === 'host_agent' ? [badge('Host agent', s.agent_connected)] : [])];
    if (s.maintenance) {
      const notice = document.createElement('span');
      notice.className = 'badge';
      notice.textContent = 'Backup or restore in progress';
      healthBadges.push(notice);
    }
    $('health').replaceChildren(...healthBadges);
    $('wizard-health').replaceChildren(badge('App', s.app_ready), badge('Qdrant', s.qdrant_ready), ...(s.agent_managed ? [badge('Host agent', s.agent_connected)] : []));
    const syncState = s.agent_error ? 'Host sync failed: ' + s.agent_error : !s.agent_connected ? s.agent_managed ? 'Automatic host agent unavailable' : 'Host folder access is not configured' : syncing ? 'Syncing documents' + (syncDetailText || '…') : s.agent_synced ? 'Host documents synchronized' : 'Waiting for host sync';
    const syncDetail = s.agent_last_sync_at ? ` · Last sync: ${new Date(s.agent_last_sync_at * 1000).toLocaleString('en-GB')}` : '';
    $('agent-status').textContent = s.config.source_mode === 'host_agent' ? syncState + syncDetail : '';
    $('index-state').textContent = s.index_running ? 'Indexing in progress…' : s.config.initial_index_skipped && (!s.last_result || s.last_result.dry_run) ? 'Initial indexing has not run yet.' : s.last_result ? `${s.last_result.dry_run ? 'Dry run' : 'Indexing'} ${s.last_result.exit_code === 0 ? 'completed' : s.index_error_count ? 'finished with document errors' : 'failed'} · ${new Date(s.last_result.finished_at * 1000).toLocaleString('en-GB')}` : 'No run recorded.';
    $('index-summary').classList.toggle('completed', !s.index_running && s.last_result?.exit_code === 0 && !(s.config.initial_index_skipped && s.last_result.dry_run));
    $('index-summary').classList.toggle('failed', !s.index_running && s.last_result && s.last_result.exit_code !== 0);
    const initialIndexFailed = s.config.setup_step < 7 && !s.index_running && s.last_result && !s.last_result.dry_run && s.last_result.exit_code !== 0;
    const syncSummary = syncing ? `Document sync in progress${syncDetailText}. Search is available when the sync completes.` : '';
    $('overview-state').textContent = s.index_running ? 'Indexing is in progress.' : initialIndexFailed ? 'Initial indexing failed. Open Indexing to review the log and retry.' : s.config.initial_index_skipped ? 'Initial indexing was postponed. Your documents will be searchable after you run it.' : syncSummary;
    $('overview-state').classList.toggle('hidden', !$('overview-state').textContent);
    $('overview-open-indexing').classList.toggle('hidden', !s.index_running && !s.config.initial_index_skipped && !initialIndexFailed);
    $('index-open-dashboard').classList.toggle('hidden', s.config.setup_step >= 7);
    $('log').textContent = s.log || 'No run yet.'; $('setup-log').textContent = s.log || 'No run yet.';
    $('dry').disabled = s.index_running; $('run').disabled = s.index_running || !s.qdrant_ready || !s.source_ready;
    $('rebuild').disabled = $('run').disabled;
    $('run').textContent = s.config.active_embedding_model && s.config.active_embedding_model !== s.config.embedding_model ? 'Index with selected model' : s.last_result && !s.last_result.dry_run && s.last_result.exit_code !== 0 ? 'Retry incremental update' : s.config.initial_index_skipped ? 'Start initial indexing' : 'Run incremental update';
    document.querySelectorAll('[data-qdrant]').forEach(x => x.disabled = !s.qdrant_managed);
    $('dashboard-qdrant-start').classList.toggle('hidden', s.qdrant_ready || !s.qdrant_managed);
    $('wizard-qdrant').classList.toggle('hidden', s.qdrant_ready || !s.qdrant_managed);
    $('wizard-qdrant').disabled = !s.qdrant_managed;
    $('next-run').textContent = s.next_run_at ? 'Next update: ' + new Date(s.next_run_at * 1000).toLocaleString('en-GB') : 'Scheduling is off';
    if (document.activeElement !== $('interval')) $('interval').value = s.config.interval_hours;
    if (s.index_running && s.index_progress?.stage === 'dry_run') $('dry-result').textContent = 'Dry run in progress…';
    else if (s.last_result?.dry_run && !s.index_running) $('dry-result').textContent = s.last_result.exit_code === 0 ? 'Dry run passed. You can continue to indexing.' : 'Dry run failed. Review the log and retry.';
    $('dry-next').disabled = !(s.last_result?.dry_run && !s.index_running && s.last_result.exit_code === 0);
    $('dry-run-button').disabled = s.index_running;
    $('initial-index-button').disabled = s.index_running || !s.qdrant_ready || !s.source_ready;
    $('skip-initial-index').disabled = s.index_running;
    const showSetup = !s.config.onboarding_complete && location.pathname !== '/dashboard/settings';
    $('wizard').classList.toggle('hidden', !showSetup); $('dashboard').classList.toggle('hidden', showSetup);
    $('setup-progress-card').classList.toggle('hidden', !showSetup);
    $('settings-back-to-setup').classList.toggle('hidden', Boolean(s.config.onboarding_complete));
    $('dashboard-nav').classList.toggle('hidden', !s.config.onboarding_complete);
    refreshError = null;
  } catch (e) {
    if (refreshError !== e.message) message(e.message, true);
    refreshError = e.message;
  } finally {
    refreshing = false;
  }
}
async function start() {
  try {
    const auth = await api('/api/auth');
    $('boot-view').classList.add('hidden');
    if (!auth.authenticated) { $('login-view').classList.remove('hidden'); return; }
    authRequired = auth.required;
    $('app-view').classList.remove('hidden'); $('logout').classList.toggle('hidden', !auth.required);
    $('auth-state').textContent = auth.required ? 'Login enabled' : 'Login disabled';
    current = await api('/api/status');
    $('login-enabled').checked = auth.required;
    $('login-disabled').checked = !auth.required;
    toggleSetupPassword();
    fillSourceInputs();
    $('interval').value = current.config.interval_hours;
    await loadPolicy();
    syncRoute(current); await refresh(); setInterval(refresh, 3000);
  } catch (e) {
    $('boot-view').classList.remove('hidden');
    $('boot-status').textContent = 'The service is starting or temporarily unreachable. Retrying automatically…';
    message(e.message, true);
    setTimeout(start, 3000);
  }
}
if (typeof window !== 'undefined') {
  window.addEventListener('popstate', () => { if (current) syncRoute(current); });
  document.addEventListener('click', event => {
    const setupLink = event.target.closest?.('[data-setup-step]');
    if (setupLink && current && !current.config.onboarding_complete) {
      event.preventDefault();
      const step = Number(setupLink.dataset.setupStep);
      if (step <= current.config.setup_step) showStep(step);
      else message('Complete the previous setup step first.', true);
      return;
    }
    const indexLink = event.target.closest?.('[data-index-nav]');
    if (indexLink) { event.preventDefault(); goIndexPage(indexLink.dataset.indexNav, true); return; }
    const link = event.target.closest?.('[data-nav-page]');
    if (!link) return;
    event.preventDefault(); goDashboardPage(link.dataset.navPage, true);
  });
}
start();
