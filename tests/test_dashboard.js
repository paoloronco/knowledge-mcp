const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ui = path.join(__dirname, '../knowledge-mcp/mcp');
const script = fs.readFileSync(path.join(ui, 'app.js'), 'utf8').replace(/start\(\);\s*$/, '');
const html = fs.readFileSync(path.join(ui, 'webui.html'), 'utf8');
const visibleText = node => [node.textContent, ...(node.children || []).map(visibleText)].join(' ');

function dashboard(config, connected = false, pathname = '/dashboard/indexing') {
  const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    value: '', textContent: '', children: [], open: false, disabled: false,
    classList: {
      classes: new Set(),
      toggle(name, force) { if (force ?? !this.classes.has(name)) this.classes.add(name); else this.classes.delete(name); },
      remove(name) { this.classes.delete(name); }, add(name) { this.classes.add(name); },
      contains(name) { return this.classes.has(name); }
    }, replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); }, scrollTo() {},
    removeAttribute(name) { if (name === 'value') this.value = undefined; }, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }
  }]));
  const calls = [];
  const makeClassList = () => ({classes: new Set(), toggle(name, force) { if (force) this.classes.add(name); else this.classes.delete(name); }, contains(name) { return this.classes.has(name); }});
  const panels = [...html.matchAll(/class="card dashboard-panel" data-page="([^"]+)"/g)].map(([, page]) => ({dataset: {page}, classList: makeClassList()}));
  const links = [...html.matchAll(/data-nav-page="([^"]+)"/g)].map(([, navPage]) => ({dataset: {navPage}, classList: makeClassList()}));
  const indexPanels = [...html.matchAll(/data-index-page="([^"]+)"/g)].map(([, indexPage]) => ({dataset: {indexPage}, classList: makeClassList()}));
  const indexLinks = [...html.matchAll(/data-index-nav="([^"]+)"/g)].map(([, indexNav]) => ({dataset: {indexNav}, classList: makeClassList(), attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; }}));
  const steps = [...html.matchAll(/class="step" data-step="([^"]+)"/g)].map(([, step]) => ({dataset: {step}, classList: makeClassList()}));
  const initialRoot = config.source_mode === 'host_agent' ? config.host_root : config.source_root;
  const status = {config: {sync_request: 0, ...config}, source_root: config.source_root, agent_connected: connected, agent_paired: connected, agent_managed: true,
    source_ready: connected || config.source_mode === 'container', agent_file_count: 2, agent_sync_request_completed: 1,
    scan_complete: false, scan_ready: false, scan_running: false, scan_checked: 0, scan_eligible_count: 0, scan_eligible_preview: [],
    document_paths: initialRoot ? [initialRoot] : []};
  const location = {pathname, reloads: 0, reload() { this.reloads++; }};
  const context = vm.createContext({
    document: {getElementById: id => elements[id] || null, querySelectorAll: selector => selector === '.dashboard-panel' ? panels : selector === '[data-nav-page]' ? links : selector === '.index-pane' ? indexPanels : selector === '[data-index-nav]' ? indexLinks : selector === '.step' ? steps : [],
      createElement: () => ({className: '', textContent: '', children: [], append(...nodes) { this.children.push(...nodes); }, setAttribute() {}}), activeElement: null},
    token: 'test-control-token', Date, clearTimeout() {}, location, history: {pushState(_, __, url) { location.pathname = url; }, replaceState(_, __, url) { location.pathname = url; }},
    setTimeout(callback, delay) { if (delay === 2000) callback(); },
    async fetch(route, options) {
      const body = options?.body ? JSON.parse(options.body) : null;
      calls.push({route, body});
      let result;
      if (route === '/api/config') {
        const paths = body.document_paths;
        status.document_paths = paths || status.document_paths;
        status.config = {...status.config, source_selection: body.source_selection, host_root: paths?.[0] || status.config.host_root, source_mode: 'host_agent'};
        status.source_root = status.config.source_root;
        result = status.config;
      } else if (route === '/api/embedding-model') {
        status.config = {...status.config, embedding_model: body.model};
        result = status.config;
      } else if (route === '/api/security') {
        status.config.setup_step = Math.max(1, status.config.setup_step);
        result = {enabled: body.enabled};
      } else if (route === '/api/status') result = status;
      else if (route === '/api/policy') result = body ? {saved: true} : {policy: {exclude_files: []}, content: 'exclude_files: []'};
      else if (route === '/api/folder/check') result = {reachable: true, mode: 'host_agent'};
      else if (route === '/api/scan/files') result = {files: ['notes/first.md', 'notes/second.md', 'notes/third.md']};
      else if (route === '/api/index/errors') result = {errors: status.index_errors || [], skipped: status.index_skipped || [], origin: status.index_error_origin || 'current'};
      else if (route === '/api/agent/refresh') result = {sync_request: 1};
      else if (route === '/api/scan') result = {requested: true};
      else if (route === '/api/onboarding/progress') result = {setup_step: body.step};
      else if (route === '/api/onboarding/skip-index') {
        status.config = {...status.config, onboarding_complete: true, setup_step: 7, initial_index_skipped: true};
        result = {complete: true, initial_index_skipped: true};
      }
      else if (route === '/api/access') {
        status.mcp_auth = {enabled: body.mcp_auth_enabled, token: body.mcp_auth_token || 'generated-access-token'};
        status.config.acl_allow_roots = body.acl_allow_roots;
        status.config.acl_deny_directories = body.acl_deny_directories;
        status.config.lexical_bigrams = body.lexical_bigrams;
        result = {...body, mcp_auth_token: status.mcp_auth.token};
      }
      else if (route === '/api/index') result = {started: true};
      else throw Error('Unexpected request: ' + route);
      return {ok: true, json: async () => result};
    }
  });
  vm.runInContext(script, context);
  context.initial = status;
  vm.runInContext('current = initial; wizardStep = 2; fillSourceInputs();', context);
  return {elements, calls, context, status, panels, links, indexPanels, indexLinks, steps, location};
}

const legacy = {source_selection: 'auto', source_mode: 'host_agent', source_root: '/data/host-documents', host_root: '', setup_step: 2, folders: []};

test('Settings is a dedicated page that can restore a backup before onboarding', async () => {
  const {context, elements, panels, location} = dashboard(legacy, false, '/dashboard/settings');
  await vm.runInContext('refresh()', context);
  assert.equal(location.pathname, '/dashboard/settings');
  assert.equal(panels.find(p => p.classList.contains('active')).dataset.page, 'settings');
  assert.equal(elements.dashboard.classList.contains('hidden'), false);
  assert.equal(elements.wizard.classList.contains('hidden'), true);
  assert.equal(elements['setup-progress-card'].classList.contains('hidden'), true);
  assert.equal(elements['settings-back-to-setup'].classList.contains('hidden'), false);
  location.pathname = '/';
  await vm.runInContext('refresh()', context);
  assert.equal(location.pathname, '/setup/folders');
  assert.equal(elements.dashboard.classList.contains('hidden'), true);
  assert.equal(elements.wizard.classList.contains('hidden'), false);

  const completed = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  for (const page of ['settings', 'overview', 'indexing', 'folders', 'policy']) {
    vm.runInContext(`goDashboardPage('${page}', true)`, completed.context);
    await vm.runInContext('refresh()', completed.context);
    assert.equal(completed.panels.find(p => p.dataset.page === 'settings').classList.contains('active'), page === 'settings');
  }
  assert.equal(completed.elements['settings-back-to-setup'].classList.contains('hidden'), true);
});

test('Indexing has separate model and run pages with stable direct links', async () => {
  const {context, indexPanels, indexLinks, location} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  await vm.runInContext('refresh()', context);
  assert.equal(indexPanels.find(p => p.dataset.indexPage === 'model').classList.contains('hidden'), true);
  assert.equal(indexPanels.find(p => p.dataset.indexPage === 'run').classList.contains('hidden'), false);
  vm.runInContext("goIndexPage('model', true)", context);
  await vm.runInContext('refresh()', context);
  assert.equal(location.pathname, '/dashboard/indexing/model');
  assert.equal(indexPanels.find(p => p.dataset.indexPage === 'run').classList.contains('hidden'), true);
  assert.equal(indexLinks.find(p => p.dataset.indexNav === 'model').attributes['aria-current'], 'page');
  vm.runInContext("goIndexPage('run', true)", context);
  assert.equal(location.pathname, '/dashboard/indexing');
  const direct = dashboard({...legacy, onboarding_complete: true, setup_step: 7}, false, '/dashboard/indexing/model');
  await vm.runInContext('refresh()', direct.context);
  assert.equal(direct.location.pathname, '/dashboard/indexing/model');
  assert.equal(direct.indexPanels.find(p => p.dataset.indexPage === 'model').classList.contains('hidden'), false);
});

test('completed indexing displays date and count together and hides the full progress bar', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.last_result = {dry_run: false, exit_code: 0, finished_at: 1791142471};
  status.index_progress = {stage: 'complete', completed: 4917, total: 4917};
  status.index_running = false;
  await vm.runInContext('refresh()', context);
  assert.match(elements['index-state'].textContent, /^Indexing completed · /);
  assert.equal(elements['index-progress-label'].textContent, 'Completed: 4917 (100%)');
  assert.equal(elements['index-summary'].classList.contains('completed'), true);
  assert.equal(elements['index-progress'].classList.contains('hidden'), true);
  status.index_running = true;
  status.index_progress = {stage: 'indexing', completed: 100, total: 4917};
  await vm.runInContext('refresh()', context);
  assert.equal(elements['index-progress'].classList.contains('hidden'), false);
  assert.equal(elements['index-summary'].classList.contains('completed'), false);
  assert.match(elements['index-progress-label'].textContent, /Indexing documents: 100 of 4917/);
});

test('rebuild requires confirmation and uses the selected model indexing endpoint', async () => {
  const {elements, context, calls, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7}, true);
  status.qdrant_ready = true;
  context.confirm = () => false;
  await vm.runInContext('runIndex(false, true)', context);
  assert.equal(calls.some(c => c.route === '/api/index'), false);
  context.confirm = () => true;
  await vm.runInContext('runIndex(false, true)', context);
  assert.equal(calls.find(c => c.route === '/api/index').body.rebuild, true);
  status.index_running = true;
  await vm.runInContext('refresh()', context);
  assert.equal(elements.rebuild.disabled, true);
  assert.equal(elements.run.disabled, true);
});

test('backup downloads an archive and restore requires confirmation before upload', async () => {
  const {elements, context} = dashboard(legacy);
  const requests = [], file = {name: 'backup.tar.gz'};
  let clicked = false;
  context.document.createElement = () => ({click() { clicked = true; }});
  context.fetch = async (route, options) => {
    requests.push({route, options});
    return {ok: true, json: async () => ({url: '/api/backup/download?id=one-use-key', restored: true, restarting: true})};
  };
  await vm.runInContext('downloadBackup()', context);
  assert.equal(clicked, true);
  assert.equal(requests[0].route, '/api/backup');
  assert.equal(requests[0].options.headers['X-Control-Token'], 'test-control-token');
  assert.doesNotMatch(script, /response\.blob\(\)/);
  assert.doesNotMatch(html, /<h3>NVIDIA GPU<\/h3>/);
  assert.equal(elements['backup-download'].disabled, false);
  elements['backup-file'].files = [file];
  context.confirm = () => false;
  await vm.runInContext('restoreBackup()', context);
  assert.equal(requests.length, 1);
  context.confirm = () => true;
  await vm.runInContext('restoreBackup()', context);
  assert.equal(requests[1].route, '/api/restore');
  assert.equal(requests[1].options.body, file);
  assert.match(elements['backup-status'].textContent, /restarting/);
});

test('model choice shows hardware guidance and keeps active search until indexing', async () => {
  const {elements, calls, context, status} = dashboard({...legacy, embedding_model: 'e5-small', active_embedding_model: 'e5-small', onboarding_complete: true, setup_step: 7});
  status.embedding_models = {
    'e5-small': {name: 'E5 small', use_case: 'Small VMs', resources: 'Low', speed: 'Fast', dimensions: 384, note: 'Default'},
    'bge-m3': {name: 'BGE-M3', use_case: 'Larger VMs', resources: 'High', speed: 'Slow', dimensions: 1024, note: 'Dense only'}
  };
  await vm.runInContext('refresh()', context);
  assert.equal(elements['dashboard-model-options'].children.length, 2);
  const model = elements['dashboard-model-options'].children[1].children[0];
  assert.match(visibleText(model), /CPU speed Slow/);
  assert.match(visibleText(model), /Memory High/);
  assert.match(visibleText(model), /Vector size 1024 dims/);
  assert.match(visibleText(model), /Runs on CPU \/ GPU/);
  assert.match(visibleText(elements['dashboard-model-options'].children[0]), /Recommended Selected/);
  assert.match(html, /\.model-option:disabled\{opacity:1/);
  await elements['dashboard-model-options'].children[1].children[0].onclick();
  assert.deepEqual(calls.find(x => x.route === '/api/embedding-model').body, {model: 'bge-m3'});
  assert.equal(status.config.active_embedding_model, 'e5-small');
  assert.match(elements['dashboard-model-state'].textContent, /separate index/);
  assert.equal(elements.run.textContent, 'Index with selected model');
});

test('GPU status cards distinguish acceleration, CPU fallback and unavailable CUDA', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.gpu = {checking: false, detected: true, usable: true, cuda_runtime: '12.6', reason: 'CUDA ready.', devices: [
    {name: 'NVIDIA GeForce RTX 3070', driver: '617.14', memory_free_mb: 4720, memory_total_mb: 8192}
  ]};
  status.embedding_device_preference = 'auto';
  await vm.runInContext('refresh()', context);
  assert.match(visibleText(elements['overview-gpu-status']), /Driver 617\.14/);
  assert.match(visibleText(elements['index-gpu-status']), /GPU embeddings Active/);
  assert.match(elements['wizard-gpu-status'].className, /gpu-status-ready/);
  status.gpu = {...status.gpu, usable: false, cuda_runtime: null, reason: 'This image has CPU-only PyTorch.'};
  await vm.runInContext('refresh()', context);
  assert.match(visibleText(elements['overview-gpu-status']), /CPU embeddings GPU unavailable/);
  assert.match(visibleText(elements['overview-gpu-status']), /CPU-only PyTorch/);
  status.embedding_device_preference = 'cuda';
  await vm.runInContext('refresh()', context);
  assert.match(visibleText(elements['overview-gpu-status']), /GPU unavailable/);
  assert.match(elements['overview-gpu-status'].className, /gpu-status-warning/);
  status.embedding_device_preference = 'cpu';
  await vm.runInContext('refresh()', context);
  assert.match(visibleText(elements['overview-gpu-status']), /CPU embeddings Selected/);
  status.embedding_device_preference = 'auto';
  status.gpu = {checking: false, detected: false, usable: false, devices: [], reason: 'No NVIDIA GPU is exposed to the application container.'};
  await vm.runInContext('refresh()', context);
  assert.match(visibleText(elements['overview-gpu-status']), /CPU embeddings Active/);
  assert.match(visibleText(elements['overview-gpu-status']), /No NVIDIA GPU available in this container/);
});

test('indexing guidance is concise with technical details in accessible tooltips', () => {
  assert.match(html, /Choose the model that fits your documents and hardware/);
  assert.doesNotMatch(html, /Incremental updates process only changes/);
  assert.match(html, /aria-describedby="model-help"/);
  assert.match(html, /<button id="run"[^>]*aria-describedby="index-help"/);
  assert.match(html, /\.info-tip:hover \.info-popover,\.info-tip:focus-within \.info-popover/);
  assert.equal(html.includes('Each run compares file hashes.'), false);
});

test('indexing errors show the affected file, retry action, downloads and ignore option', async () => {
  const {elements, calls, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.last_result = {dry_run: false, exit_code: 1, finished_at: 1};
  status.index_error_count = 1;
  status.index_error_origin = 'current';
  status.index_errors = [{source: 'notes/bad.docx', stage: 'indexing', error_type: 'PackageNotFoundError', message: 'Invalid package', can_ignore: true, ignored: false}];
  status.index_log_available = true;
  status.index_stages_available = true;
  await vm.runInContext('refresh()', context);
  assert.match(elements['index-errors-summary'].textContent, /1 document has errors/);
  assert.equal(elements['index-errors-list'].children[0].children[0].textContent, 'notes/bad.docx');
  assert.equal(elements.run.textContent, 'Retry incremental update');
  assert.equal(elements['download-index-full'].classList.contains('hidden'), false);
  assert.equal(elements['download-index-stages'].classList.contains('hidden'), false);
  vm.runInContext('loadPolicy = async () => {}', context);
  await elements['index-errors-list'].children[0].children[2].onclick();
  assert.deepEqual(calls.find(x => x.route === '/api/policy' && x.body).body.policy.exclude_files, ['notes/bad.docx']);
});

test('older Office lock errors are marked as already excluded and lack a status log', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.last_result = {dry_run: false, exit_code: 1, finished_at: 1};
  status.index_error_count = 1;
  status.index_error_origin = 'legacy';
  status.index_errors = [{source: 'notes/~$draft.docx', stage: 'indexing', error_type: 'PackageNotFoundError', message: 'Invalid package', auto_excluded: true, can_ignore: false}];
  status.index_log_available = true;
  status.index_stages_available = false;
  await vm.runInContext('refresh()', context);
  assert.match(elements['index-errors-summary'].textContent, /older error log/);
  assert.match(elements['index-errors-list'].children[0].children[2].textContent, /Automatically excluded/);
  assert.equal(elements['download-index-stages'].classList.contains('hidden'), true);
  assert.equal(elements['index-stages-note'].classList.contains('hidden'), false);
});

test('adding a folder checks reachability and shows it in the list', async () => {
  const {elements, calls, context} = dashboard(legacy);
  elements['setup-source-root'].value = '/mnt/documents';
  await vm.runInContext('addFolder(true)', context);
  assert.deepEqual(calls.find(x => x.route === '/api/folder/check').body, {path: '/mnt/documents', source_selection: 'auto'});
  assert.deepEqual(calls.find(x => x.route === '/api/config').body, {
    document_paths: ['/mnt/documents'], source_selection: 'auto', interval_hours: 0
  });
  assert.equal(elements['setup-folder-list'].children[0].children[0].textContent, '/mnt/documents');
  assert.equal(elements['setup-source-root'].value, '');
  assert.doesNotMatch(html, /host_agent\.py install|Generate pairing key|Download the host service/);
});

test('folders can be removed, then Next advances to policy without scanning', async () => {
  const {elements, calls, context} = dashboard(legacy);
  elements['setup-source-root'].value = '/home/user/My documents';
  await vm.runInContext('addFolder(true)', context);
  elements['setup-source-root'].value = '/home/user/Other documents';
  await vm.runInContext('addFolder(true)', context);
  assert.equal(elements['setup-folder-list'].children.length, 2);
  await vm.runInContext("removeFolder('/home/user/My documents')", context);
  assert.equal(elements['setup-folder-list'].children.length, 1);
  await vm.runInContext('advanceFolders()', context);
  assert.equal(calls.find(x => x.route === '/api/onboarding/progress').body.step, 3);
  assert.equal(calls.some(x => x.route === '/api/scan'), false);
});

test('eligible documents step shows scan progress and enables Next only with matches', async () => {
  const {elements, context, status} = dashboard({...legacy, source_mode: 'host_agent', host_root: '/mnt/documents'}, true);
  status.source_ready = false;
  status.scan_running = true;
  status.scan_checked = 12;
  status.scan_eligible_count = 2;
  status.agent_progress = {phase: 'scanning', checked: 12, eligible: 2, completed: 0, total: 0};
  await vm.runInContext('refresh()', context);
  assert.match(elements['setup-scan-status'].textContent, /Scanning host folder: 12 files checked, 2 eligible/);
  assert.equal(elements['setup-eligible-list'].children.length, 0);
  assert.equal(elements['setup-sync-progress'].classList.contains('hidden'), false);
  status.agent_progress = {phase: 'transferring', checked: 20, completed: 2, total: 4};
  await vm.runInContext('refresh()', context);
  assert.match(elements['setup-scan-status'].textContent, /2 of 4/);
  assert.equal(elements['setup-sync-progress'].value, 50);
  status.scan_complete = true;
  status.scan_running = false;
  status.scan_eligible_count = 0;
  status.agent_progress = null;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['setup-sync-progress'].classList.contains('hidden'), true);
  assert.equal(elements['scan-next'].disabled, true);
  status.scan_ready = true;
  status.scan_eligible_count = 2;
  status.scan_eligible_preview = ['notes/first.md', 'notes/second.md'];
  await vm.runInContext('refresh()', context);
  assert.equal(elements['scan-next'].disabled, false);
  assert.deepEqual(elements['setup-eligible-list'].children.map(x => x.textContent), ['notes/first.md', 'notes/second.md']);
  await vm.runInContext('openEligibleFiles()', context);
  assert.equal(elements['eligible-dialog'].open, true);
  assert.match(elements['eligible-all'].textContent, /notes\/third\.md/);
});

test('changing access preference preserves the document path being edited', () => {
  const {elements, context} = dashboard(legacy);
  elements['setup-source-root'].value = '/mnt/custom-documents';
  context.document.activeElement = {id: 'setup-source-mode', value: 'host_agent'};
  vm.runInContext('sourceModeChanged()', context);
  assert.equal(elements['setup-source-root'].value, '/mnt/custom-documents');
  assert.equal(elements['dashboard-source-mode'].value, 'host_agent');
});

test('Service health shows Start Qdrant only while managed Qdrant is unavailable', async () => {
  const {elements, context, status} = dashboard(legacy);
  status.qdrant_managed = true;
  status.qdrant_ready = true;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['wizard-qdrant'].classList.contains('hidden'), true);
  assert.equal(elements['dashboard-qdrant-start'].classList.contains('hidden'), true);
  status.qdrant_ready = false;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['wizard-qdrant'].classList.contains('hidden'), false);
  assert.equal(elements['dashboard-qdrant-start'].classList.contains('hidden'), false);
  status.qdrant_managed = false;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['wizard-qdrant'].classList.contains('hidden'), true);
  assert.equal(elements['dashboard-qdrant-start'].classList.contains('hidden'), true);
});

test('continuing without login saves the choice and survives a reload from setup or Settings', async () => {
  for (const [action, required] of [['setupSecurity', false], ['setupSecurity', true], ['disablePassword', true]]) {
    const {elements, calls, context, location, steps} = dashboard({...legacy, setup_step: 0}, false, '/setup/login');
    elements['login-enabled'].checked = false;
    vm.runInContext(`wizardStep = 0; authRequired = ${required}`, context);
    await vm.runInContext(`${action}()`, context);
    assert.deepEqual(calls.find(x => x.route === '/api/security').body, {enabled: false});
    assert.equal(location.reloads, 1);
    await vm.runInContext('refresh()', context);
    assert.equal(location.pathname, '/setup/health');
    assert.equal(steps.find(x => x.classList.contains('active')).dataset.step, '1');
  }
  for (const [complete, step, target] of [[true, 7, '/dashboard/settings'], [false, 3, '/setup/policy']]) {
    const {context, location} = dashboard({...legacy, onboarding_complete: complete, setup_step: step}, false, '/dashboard/settings');
    await vm.runInContext('disablePassword();', context);
    await vm.runInContext('refresh()', context);
    assert.equal(location.pathname, target);
  }
  assert.doesNotMatch(html, /Enable login to select folders on the Docker host/);
});

test('an existing dashboard password can be kept without entering another password', async () => {
  const {elements, calls, context, location} = dashboard({...legacy, setup_step: 0}, false, '/setup/login');
  elements['login-enabled'].checked = true;
  vm.runInContext('wizardStep = 0; authRequired = true', context);
  await vm.runInContext('setupSecurity()', context);
  assert.equal(calls.some(x => x.route === '/api/security'), false);
  assert.equal(location.pathname, '/setup/health');
});

test('password field is shown only when login is enabled', () => {
  const {elements, context} = dashboard({...legacy, setup_step: 0});
  elements['login-enabled'].checked = false;
  vm.runInContext('toggleSetupPassword()', context);
  assert.equal(elements['login-password-fields'].classList.contains('hidden'), true);
  assert.equal(elements['setup-login-next'].textContent, 'Continue without login');
  elements['login-enabled'].checked = true;
  vm.runInContext('toggleSetupPassword()', context);
  assert.equal(elements['login-password-fields'].classList.contains('hidden'), false);
  assert.equal(elements['setup-login-next'].textContent, 'Continue with login');
});

test('successful dry run enables Continue to indexing', async () => {
  const {elements, context, status, calls} = dashboard({...legacy, setup_step: 5}, true);
  vm.runInContext('wizardStep = 5', context);
  status.last_result = {dry_run: true, exit_code: 0, finished_at: 1};
  await vm.runInContext('refresh()', context);
  assert.equal(elements['dry-next'].disabled, false);
  assert.match(elements['dry-result'].textContent, /Dry run passed/);
  await vm.runInContext('nextStep()', context);
  assert.equal(calls.find(x => x.route === '/api/onboarding/progress').body.step, 6);
});

test('initial indexing can continue while the user follows progress from the dashboard', async () => {
  const {elements, context, status, panels, location} = dashboard({...legacy, onboarding_complete: true, setup_step: 6}, true);
  status.index_running = true;
  status.index_progress = {stage: 'indexing', completed: 25, total: 100};
  await vm.runInContext('refresh()', context);
  assert.equal(elements['setup-progress-card'].classList.contains('hidden'), true);
  assert.equal(elements['dashboard-nav'].classList.contains('hidden'), false);
  assert.equal(elements['index-open-dashboard'].classList.contains('hidden'), false);
  assert.match(elements['index-progress-label'].textContent, /25 of 100 \(25%\)/);
  assert.equal(panels.filter(x => x.classList.contains('active')).map(x => x.dataset.page).join(','), 'indexing');
  vm.runInContext("goDashboardPage('overview', true)", context);
  await vm.runInContext('refresh()', context);
  assert.equal(location.pathname, '/dashboard');
  assert.equal(panels.filter(x => x.classList.contains('active')).map(x => x.dataset.page).join(','), 'overview');
  assert.equal(elements['overview-indexing-progress'].classList.contains('hidden'), false);
  assert.match(elements['overview-index-progress-label'].textContent, /25 of 100 \(25%\)/);
  assert.equal(elements['overview-index-progress'].value, 25);
  status.index_progress.completed = 60;
  await vm.runInContext('refresh()', context);
  assert.equal(location.pathname, '/dashboard');
  assert.match(elements['overview-index-progress-label'].textContent, /60 of 100 \(60%\)/);
  assert.equal(elements['overview-index-progress'].value, 60);
  status.index_running = false;
  status.last_result = {dry_run: false, exit_code: 1, finished_at: 1};
  await vm.runInContext('refresh()', context);
  assert.match(elements['overview-state'].textContent, /Initial indexing failed/);
  assert.equal(elements['overview-open-indexing'].classList.contains('hidden'), false);
  status.config.setup_step = 7;
  status.last_result.exit_code = 0;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['overview-indexing-progress'].classList.contains('hidden'), true);
  assert.equal(elements['index-open-dashboard'].classList.contains('hidden'), true);
  assert.equal(location.pathname, '/dashboard');
  assert.equal(panels.filter(x => x.classList.contains('active')).map(x => x.dataset.page).join(','), 'overview');
});

test('dashboard contains service controls and folder sync detail stays with folders', async () => {
  const {elements, context, status, panels, location} = dashboard({...legacy, onboarding_complete: true, setup_step: 7}, true, '/dashboard');
  status.agent_synced = true;
  status.agent_last_sync_at = 1;
  await vm.runInContext('refresh()', context);
  assert.equal(panels.find(x => x.classList.contains('active')).dataset.page, 'overview');
  assert.deepEqual(elements['health'].children.map(x => x.textContent.split(':')[0]), ['App', 'Qdrant', 'MCP', 'Documents', 'Host agent']);
  assert.equal(elements['overview-open-indexing'].classList.contains('hidden'), true);
  assert.match(elements['agent-status'].textContent, /Host documents synchronized/);
  assert.equal(html.includes('data-page="services"'), false);
  assert.equal(html.includes('data-nav-page="services"'), false);
  assert.equal(html.includes('id="source-root"'), false);
  assert.equal(html.includes('Wait for indexing to finish before starting MCP.'), false);
  assert.equal(html.includes('Start MCP after indexing'), false);
  location.pathname = '/dashboard/services';
  vm.runInContext('syncRoute(current)', context);
  assert.equal(location.pathname, '/dashboard');
});

test('setup routes can be opened directly and navigation updates the URL', () => {
  const {context, location, steps} = dashboard({...legacy, setup_step: 3}, false, '/setup/health');
  vm.runInContext('syncRoute(current)', context);
  assert.equal(location.pathname, '/setup/health');
  assert.equal(steps.find(x => x.classList.contains('active')).dataset.step, '1');
  vm.runInContext('showStep(2)', context);
  assert.equal(location.pathname, '/setup/folders');
  vm.runInContext('backStep()', context);
  assert.equal(location.pathname, '/setup/health');
  location.pathname = '/setup/indexing';
  vm.runInContext('syncRoute(current)', context);
  assert.equal(location.pathname, '/setup/policy');
  assert.equal(steps.find(x => x.classList.contains('active')).dataset.step, '3');
});

test('skipping initial indexing opens the dashboard and keeps indexing available', async () => {
  const {elements, calls, context, status, panels, location} = dashboard({...legacy, setup_step: 6}, true, '/setup/indexing');
  await vm.runInContext('skipInitialIndex()', context);
  assert.equal(calls.some(x => x.route === '/api/onboarding/skip-index'), true);
  assert.equal(location.pathname, '/dashboard');
  assert.equal(status.config.initial_index_skipped, true);
  assert.equal(elements['dashboard-nav'].classList.contains('hidden'), false);
  assert.equal(panels.find(x => x.classList.contains('active')).dataset.page, 'overview');
  assert.match(elements['overview-state'].textContent, /postponed/);
  assert.equal(elements['overview-open-indexing'].classList.contains('hidden'), false);
  vm.runInContext("goDashboardPage('indexing', true)", context);
  assert.equal(location.pathname, '/dashboard/indexing');
  assert.equal(elements['run'].textContent, 'Start initial indexing');
  await vm.runInContext('runIndex(false)', context);
  assert.equal(calls.some(x => x.route === '/api/index' && x.body.dry_run === false), true);
});

test('documents health reports an in-progress host sync instead of unavailable', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7}, true, '/dashboard');
  const documents = () => elements['health'].children.find(x => x.textContent.startsWith('Documents'));
  status.source_ready = false;
  status.agent_syncing = true;
  status.agent_progress = {phase: 'scanning', checked: 120, eligible: 35, completed: 0, total: 0};
  await vm.runInContext('refresh()', context);
  assert.match(documents().textContent, /syncing/);
  assert.match(documents().className, /pending/);
  assert.match(elements['agent-status'].textContent, /120 files checked, 35 eligible/);
  assert.match(elements['overview-state'].textContent, /sync in progress/i);
  status.agent_progress = {phase: 'transferring', checked: 155, eligible: 35, completed: 12, total: 35};
  await vm.runInContext('refresh()', context);
  assert.match(elements['agent-status'].textContent, /12 of 35 documents transferred/);
  assert.match(elements['overview-state'].textContent, /12 of 35/);
  status.source_ready = true;
  status.agent_syncing = false;
  status.agent_progress = null;
  status.agent_synced = true;
  await vm.runInContext('refresh()', context);
  assert.match(documents().textContent, /ready/);
  assert.equal(elements['overview-state'].textContent, '');
  assert.match(elements['agent-status'].textContent, /Host documents synchronized/);
});

test('maintenance is surfaced in the health badges while polling stays responsive', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.maintenance = true;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['health'].children.some(x => /Backup or restore/.test(x.textContent)), true);
  status.maintenance = false;
  await vm.runInContext('refresh()', context);
  assert.equal(elements['health'].children.some(x => /Backup or restore/.test(x.textContent)), false);
  assert.deepEqual(elements['health'].children.map(x => x.textContent.split(':')[0]), ['App', 'Qdrant', 'MCP', 'Documents', 'Host agent']);
});

test('readiness banner explains startup phases, progress and the next action', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.readiness = {
    phase: 'starting', title: 'Starting up', detail: 'The vector database is warming up.',
    ready: false, tone: 'busy', spinner: true,
    steps: [{label: 'Database', state: 'active'}, {label: 'MCP server', state: 'pending'}],
    action: {label: 'Start Qdrant', kind: 'start_qdrant'}
  };
  await vm.runInContext('refresh()', context);
  assert.equal(elements['service-banner'].classList.contains('hidden'), false);
  assert.equal(elements['service-banner'].classList.contains('service-banner-busy'), true);
  assert.equal(elements['service-banner'].classList.contains('spinning'), true);
  assert.match(elements['service-banner-title'].textContent, /Starting up/);
  assert.match(elements['service-banner-detail'].textContent, /warming up/);
  assert.match(elements['service-banner-steps'].textContent, /Database: in progress/);
  assert.match(elements['service-banner-steps'].textContent, /MCP server: waiting/);
  assert.equal(elements['service-banner-action'].textContent, 'Start Qdrant');
  assert.equal(elements['service-banner-action'].classList.contains('hidden'), false);
  status.readiness = {
    phase: 'ready', title: 'Ready', detail: 'Your documents are searchable.',
    ready: true, tone: 'ready', spinner: false,
    steps: [{label: 'Database', state: 'done'}], action: null
  };
  await vm.runInContext('refresh()', context);
  assert.equal(elements['service-banner'].classList.contains('service-banner-ready'), true);
  assert.equal(elements['service-banner-action'].classList.contains('hidden'), true);
});

test('the readiness banner stays hidden when the backend does not report it', async () => {
  const {elements, context} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  await vm.runInContext('refresh()', context);
  assert.equal(elements['service-banner'].classList.contains('hidden'), true);
});

test('connection settings expose the MCP endpoint, version and update command', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.mcp_port = 8100;
  status.version = '2.2.0';
  await vm.runInContext('refresh()', context);
  assert.equal(elements['mcp-endpoint'].value, 'http://HOST_IP:8100/mcp');
  assert.equal(elements['app-version'].textContent, '2.2.0');
  assert.match(elements['update-command'].value, /self-update\.sh/);
});

test('indexing details open in a separate dialog with errors and skipped documents', async () => {
  const {elements, context, status} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.index_error_count = 1;
  status.index_errors = [{source: 'notes/bad.docx', stage: 'indexing', error_type: 'ValueError', message: 'Cannot parse', can_ignore: true, ignored: false}];
  status.index_skipped_count = 2;
  status.index_skipped = [
    {source: 'scans/a.pdf', stage: 'indexing', error_type: 'No extractable text', message: 'Scanned PDF', can_ignore: true, ignored: false},
    {source: 'scans/b.pdf', stage: 'indexing', error_type: 'No extractable text', message: 'Scanned PDF', can_ignore: true, ignored: false}
  ];
  await vm.runInContext('refresh()', context);
  assert.equal(elements['index-errors-card'].classList.contains('hidden'), false);
  assert.match(elements['index-errors-summary'].textContent, /2 documents were skipped/);
  assert.match(elements['index-errors-summary'].textContent, /1 document has errors/);
  assert.match(elements['index-errors-all'].textContent, /View details/);
  assert.equal(elements['index-errors-list'].children.length, 1);
  assert.equal(elements['index-skipped-list'].children.length, 2);
  await vm.runInContext('openIndexErrors()', context);
  assert.equal(elements['index-errors-dialog'].open, true);
  assert.equal(elements['index-errors-count'].textContent, '1');
  assert.equal(elements['index-skipped-count'].textContent, '2');
  assert.equal(elements['index-errors-empty'].classList.contains('hidden'), true);
  assert.equal(elements['index-skipped-empty'].classList.contains('hidden'), true);
  assert.equal(elements['index-ignore-all-skipped'].classList.contains('hidden'), false);
});

test('all skipped documents can be excluded in one action', async () => {
  const {context, status, calls} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  status.index_skipped_count = 2;
  status.index_skipped = [{source: 'scans/a.pdf'}, {source: 'scans/b.pdf'}];
  context.confirm = () => true;
  await vm.runInContext('loadPolicy = async () => {}; ignoreAllSkipped()', context);
  const writes = calls.filter(x => x.route === '/api/policy' && x.body && x.body.policy);
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body.policy.exclude_files, ['scans/a.pdf', 'scans/b.pdf']);
});

test('a global busy indicator reflects in-flight actions', async () => {
  const {elements, context} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  await vm.runInContext('pendingActions = 2; renderBusy();', context);
  assert.equal(elements['global-busy'].classList.contains('hidden'), false);
  await vm.runInContext('pendingActions = 0; renderBusy();', context);
  assert.equal(elements['global-busy'].classList.contains('hidden'), true);
});

test('access control settings render and save to /api/access', async () => {
  assert.match(html, /id="mcp-auth-enabled"/);
  assert.match(html, /id="acl-allow-roots"/);
  assert.match(html, /id="acl-deny-dirs"/);
  assert.match(html, /id="lexical-bigrams"/);
  assert.match(html, /onclick="saveAccess\(\)"/);
  const {elements, context, calls} = dashboard({...legacy, onboarding_complete: true, setup_step: 7});
  await vm.runInContext('refresh()', context);
  assert.equal(elements['mcp-auth-enabled'].checked, false);
  elements['mcp-auth-enabled'].checked = true;
  elements['mcp-auth-token'].value = 'my-secret-token';
  elements['acl-allow-roots'].value = '/mnt/docs\n/mnt/shared';
  elements['acl-deny-dirs'].value = 'hr\nprivate';
  elements['lexical-bigrams'].checked = true;
  await vm.runInContext('saveAccess()', context);
  const access = calls.find(x => x.route === '/api/access');
  assert.ok(access);
  assert.equal(access.body.mcp_auth_enabled, true);
  assert.equal(access.body.mcp_auth_token, 'my-secret-token');
  assert.deepEqual(access.body.acl_allow_roots, ['/mnt/docs', '/mnt/shared']);
  assert.deepEqual(access.body.acl_deny_directories, ['hr', 'private']);
  assert.equal(access.body.lexical_bigrams, true);
  assert.match(elements['access-status'].textContent, /access token/);
});
