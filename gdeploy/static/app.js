'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const page = $('#page');
  const state = {
    session: null, settings: null, inventory: null, deployments: [], detail: null,
    route: 'deployments', detailId: null, routeEpoch: 0, pollBusy: false,
    search: '', filter: 'all', wizard: null, secrets: null, secretTimer: null,
    secretDeadline: 0, secretRequest: 0, toastTimer: null,
    setupBusy: false, mediaUpload: null, setupTab: null,
  };
  const roles = {
    ubuntu: {name: 'OS only', short: 'OS only', description: 'Install the operating system without additional software.', cpu: 2, ram: 4, disk: 40, icon: 'terminal'},
    elasticsearch: {name: 'Elasticsearch', short: 'Elasticsearch', description: 'Search and analytics, with TLS enabled.', cpu: 2, ram: 8, disk: 60, icon: 'layers'},
    kibana: {name: 'Kibana', short: 'Kibana', description: 'Visualize your data. Connected to Elasticsearch.', cpu: 2, ram: 4, disk: 40, icon: 'chart'},
    splunk: {name: 'Splunk Enterprise', short: 'Splunk', description: 'Search, monitor, and analyze machine data.', cpu: 4, ram: 8, disk: 60, icon: 'activity'},
  };
  const stageOrder = ['queued', 'preflight', 'preparing', 'creating', 'installing_os', 'installing_software', 'verifying', 'completed'];
  const stageNames = {queued: 'Waiting in queue', preflight: 'Checking prerequisites', preparing: 'Preparing installation media', creating: 'Creating virtual machines', installing_os: 'Install operating system', installing_software: 'Installing software', verifying: 'Verifying services', completed: 'Ready to use', failed: 'Deployment failed', interrupted: 'Deployment interrupted', cleaning: 'Removing deployment resources', cleanup_failed: 'Cleanup needs attention', reverted: 'Resources removed'};
  const statusNames = {queued: 'Queued', running: 'In progress', completed: 'Completed', failed: 'Failed', interrupted: 'Interrupted', cleaning: 'Cleaning up', cleanup_failed: 'Cleanup failed', reverted: 'Reverted'};
  const failureStatuses = new Set(['failed', 'interrupted', 'cleanup_failed']);
  const busyStatuses = new Set(['queued', 'running', 'cleaning']);
  const icons = {
    plus: [['path', {d: 'M12 5v14M5 12h14'}]],
    arrow: [['path', {d: 'M5 12h14M13 6l6 6-6 6'}]],
    back: [['path', {d: 'M19 12H5M11 6l-6 6 6 6'}]],
    close: [['path', {d: 'm6 6 12 12M18 6 6 18'}]],
    check: [['path', {d: 'm5 12 4 4L19 6'}]],
    checkCircle: [['circle', {cx: 12, cy: 12, r: 9}], ['path', {d: 'm8 12 3 3 5-6'}]],
    alert: [['path', {d: 'm12 3 10 17H2L12 3Z'}], ['path', {d: 'M12 9v4M12 16h.01'}]],
    server: [['rect', {x: 3, y: 3, width: 18, height: 7, rx: 2}], ['rect', {x: 3, y: 14, width: 18, height: 7, rx: 2}], ['path', {d: 'M7 6.5h.01M7 17.5h.01M15 6.5h3M15 17.5h3'}]],
    grid: [['rect', {x: 3, y: 3, width: 7, height: 7, rx: 1}], ['rect', {x: 14, y: 3, width: 7, height: 7, rx: 1}], ['rect', {x: 3, y: 14, width: 7, height: 7, rx: 1}], ['rect', {x: 14, y: 14, width: 7, height: 7, rx: 1}]],
    clock: [['circle', {cx: 12, cy: 12, r: 9}], ['path', {d: 'M12 7v5l3 2'}]],
    terminal: [['rect', {x: 3, y: 4, width: 18, height: 16, rx: 2}], ['path', {d: 'm7 9 3 3-3 3M13 15h4'}]],
    layers: [['path', {d: 'm12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5'}]],
    chart: [['path', {d: 'M4 4v16h17M9 15V9M14 15V5M19 15v-4'}]],
    activity: [['path', {d: 'M2 12h5l3-8 4 16 3-8h5'}]],
    shield: [['path', {d: 'M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z'}], ['path', {d: 'm8 12 3 3 5-6'}]],
    key: [['circle', {cx: 8, cy: 8, r: 5}], ['path', {d: 'm12 12 9 9M16 16l3-3M19 19l3-3'}]],
    eye: [['path', {d: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z'}], ['circle', {cx: 12, cy: 12, r: 3}]],
    copy: [['rect', {x: 8, y: 8, width: 12, height: 13, rx: 2}], ['path', {d: 'M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3'}]],
    link: [['path', {d: 'M14 3h7v7M10 14 21 3M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5'}]],
    refresh: [['path', {d: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2l1 4M4 12l1 4a8 8 0 0 0 13 2'}]],
    info: [['circle', {cx: 12, cy: 12, r: 9}], ['path', {d: 'M12 11v6M12 7h.01'}]],
    disc: [['circle', {cx: 12, cy: 12, r: 9}], ['circle', {cx: 12, cy: 12, r: 3}]],
    folder: [['path', {d: 'M3 7V5a1 1 0 0 1 1-1h5l3 3h8a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z'}]],
    logout: [['path', {d: 'M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4M14 8l4 4-4 4M8 12h10'}]],
  };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key === 'class') node.className = value;
      else if (key === 'value') node.value = value;
      else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'readOnly' || key === 'required') node[key] = Boolean(value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function icon(name) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true'})) node.setAttribute(key, value);
    for (const [tag, attrs] of icons[name] || icons.server) {
      const part = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, value] of Object.entries(attrs)) part.setAttribute(key, value);
      node.append(part);
    }
    return node;
  }
  const button = (label, type = '', handler, iconName) => el('button', {type: 'button', class: `button ${type}`, onClick: handler}, iconName ? icon(iconName) : null, label);
  const spinner = () => el('span', {class: 'spinner', 'aria-hidden': 'true'});
  const loading = label => el('div', {class: 'loading-block', role: 'status'}, spinner(), label);
  const roleIcon = role => el('span', {class: 'role-icon'}, icon((roles[role] || roles.ubuntu).icon));
  const roleName = role => (roles[role] || {name: role || 'Virtual machine'}).name;
  function statusBadge(status) {
    const known = Object.hasOwn(statusNames, status);
    return el('span', {class: `status ${known ? `status-${status}` : ''}`}, known ? statusNames[status] : (stageNames[status] || String(status || 'Pending').replaceAll('_', ' ')));
  }
  function safeUrl(value) {
    try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; }
  }
  function serviceLink(service) {
    const url = safeUrl(service.url);
    return url ? el('a', {href: url, target: '_blank', rel: 'noopener noreferrer'}, service.name || 'Open service', icon('link')) : null;
  }
  function date(value, includeTime = false) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '—';
    return new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', ...(includeTime ? {hour: 'numeric', minute: '2-digit'} : {}), ...(parsed.getFullYear() !== new Date().getFullYear() ? {year: 'numeric'} : {})}).format(parsed);
  }
  function eventTime(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).format(parsed);
  }
  function errorText(data, fallback = 'The request could not be completed.') {
    if (typeof data === 'string') return data;
    if (Array.isArray(data)) return data.map(item => `${Array.isArray(item.loc) ? item.loc.filter(v => v !== 'body').join(' · ') + ': ' : ''}${item.msg || 'Invalid value'}`).join('\n');
    if (data && typeof data.detail === 'string') return data.detail;
    if (data?.detail && typeof data.detail.message === 'string') return data.detail.message;
    if (data && Array.isArray(data.detail)) return errorText(data.detail);
    return fallback;
  }
  async function api(path, options = {}) {
    if (setupRequired() && !['/api/session', '/api/login', '/api/logout', '/api/account/setup'].includes(path)) {
      throw new Error('Choose new administrator credentials before continuing.');
    }
    const requestSession = state.session;
    const method = options.method || 'GET';
    const headers = {Accept: 'application/json'};
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && state.session?.csrf_token) headers['X-CSRF-Token'] = state.session.csrf_token;
    let response;
    try {
      response = await fetch(path, {method, headers, credentials: 'same-origin', cache: 'no-store', ...(options.body !== undefined ? {body: JSON.stringify(options.body)} : {})});
    } catch { throw new Error('Unable to reach GDeploy. Check your connection and try again.'); }
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') showLogin('Your session has expired. Sign in to continue.');
      if (response.status === 403 && data?.detail?.code === 'credentials_change_required' && state.session && state.session === requestSession) {
        showSetup({...state.session, must_change_credentials: true}, data.detail.message);
      }
      const error = new Error(errorText(data, `Request failed (${response.status}).`));
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function notify(message) {
    clearTimeout(state.toastTimer);
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
  }
  function globalError(message) {
    const alert = $('#global-alert');
    alert.className = 'alert alert-error';
    alert.textContent = message;
    alert.hidden = !message;
  }
  function inlineError(container, message) {
    container.textContent = message;
    container.hidden = !message;
  }
  function setBusy(node, text) {
    node.disabled = true;
    node.replaceChildren(spinner(), document.createTextNode(text));
  }
  const setupRequired = () => state.session?.must_change_credentials === true;
  function resetSetupForm() {
    $('#setup-form').reset();
    for (const input of $('#setup-form').querySelectorAll('input')) input.setCustomValidity('');
    inlineError($('#setup-error'), '');
  }
  function closeWorkspace() {
    state.routeEpoch++;
    state.mediaUpload?.abort();
    hideCredentials();
    state.wizard = null;
    state.settings = null;
    state.setupTab = null;
    state.inventory = null;
    state.deployments = [];
    state.detail = null;
    state.detailId = null;
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
    page.replaceChildren();
    clearTimeout(state.toastTimer);
    $('#toast').hidden = true;
    $('#app').hidden = true;
  }
  function showLogin(message = '', options = {}) {
    state.session = null;
    closeWorkspace();
    resetSetupForm();
    $('#boot').hidden = true;
    $('#setup-screen').hidden = true;
    $('#login-screen').hidden = false;
    $('.skip-link').href = '#login-username';
    if (options.username !== undefined) $('#login-username').value = options.username;
    $('#login-password').value = '';
    inlineError($('#login-error'), message);
    inlineError($('#login-success'), options.success || '');
  }
  function showSetup(session = state.session, message = '') {
    if (!session) return;
    const entering = $('#setup-screen').hidden;
    state.session = session;
    closeWorkspace();
    $('#boot').hidden = true;
    $('#login-screen').hidden = true;
    $('#login-password').value = '';
    $('#setup-screen').hidden = false;
    $('.skip-link').href = '#setup-title';
    if (entering) {
      resetSetupForm();
      $('#setup-username').focus();
    }
    if (message) inlineError($('#setup-error'), message);
  }
  async function showApp(session) {
    if (session.must_change_credentials) { showSetup(session); return; }
    state.session = session;
    $('#boot').hidden = true;
    $('#login-screen').hidden = true;
    $('#setup-screen').hidden = true;
    resetSetupForm();
    $('#app').hidden = false;
    $('.skip-link').href = '#main-content';
    $('#account-name').textContent = session.username || 'Administrator';
    $('#account-avatar').textContent = (session.username || 'A').slice(0, 1);
    await route();
  }
  async function logout() {
    try { await api('/api/logout', {method: 'POST'}); showLogin(); }
    catch (error) { if (setupRequired()) inlineError($('#setup-error'), error.message); else globalError(error.message); }
  }
  function markNav(name) {
    for (const link of document.querySelectorAll('[data-nav]')) {
      const active = link.dataset.nav === name;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    }
  }
  function goDetail(id) { location.hash = `deployment/${encodeURIComponent(id)}`; }
  async function route() {
    if (!state.session) return;
    if (setupRequired()) { showSetup(); return; }
    const epoch = ++state.routeEpoch;
    state.mediaUpload?.abort();
    globalError('');
    hideCredentials();
    state.detail = null;
    state.detailId = null;
    page.replaceChildren(loading('Loading your workspace…'));
    const hash = location.hash.slice(1);
    const setupRoute = /^(?:settings|setup)(?:\/(connection|media))?$/.exec(hash);
    if (setupRoute) {
      state.route = 'settings';
      markNav('settings');
      $('#breadcrumb').textContent = 'Setup';
      try {
        const settings = await api('/api/settings');
        if (epoch !== state.routeEpoch) return;
        state.settings = settings;
        state.setupTab = setupRoute[1] || (settings.configured && !settings.iso_configured ? 'media' : 'connection');
        renderSettings();
      } catch (error) { if (epoch === state.routeEpoch) renderLoadError(error, route); }
    } else if (hash.startsWith('deployment/')) {
      state.route = 'detail';
      markNav('deployments');
      $('#breadcrumb').textContent = 'Deployment details';
      try {
        const id = decodeURIComponent(hash.slice('deployment/'.length));
        state.detailId = id;
        const data = await api(`/api/deployments/${encodeURIComponent(id)}`);
        if (epoch !== state.routeEpoch) return;
        state.detail = data;
        renderDetail(data);
      } catch (error) { if (epoch === state.routeEpoch) renderLoadError(error, route); }
    } else {
      state.route = 'deployments';
      markNav('deployments');
      $('#breadcrumb').textContent = 'Deployments';
      const results = await Promise.allSettled([api('/api/deployments'), api('/api/settings')]);
      if (epoch !== state.routeEpoch) return;
      if (results[1].status === 'fulfilled') state.settings = results[1].value;
      if (results[0].status === 'fulfilled') {
        state.deployments = results[0].value;
        renderOverview();
        if (results[1].status === 'rejected') globalError(results[1].reason.message);
      } else renderLoadError(results[0].reason, route);
    }
  }
  function renderLoadError(error, retry) {
    page.replaceChildren(el('div', {class: 'surface empty-state'}, el('div', {class: 'empty-icon'}, icon('alert')), el('h3', {}, 'Couldn’t load this page'), el('p', {}, error.message), button('Try again', '', retry, 'refresh')));
  }
  function heading(title, description, action) {
    return el('div', {class: 'page-heading'}, el('div', {}, el('h1', {}, title), el('p', {}, description)), action);
  }
  function renderOverview() {
    const rows = state.deployments;
    const active = rows.filter(row => busyStatuses.has(row.status)).length;
    const completed = rows.filter(row => row.status === 'completed').length;
    const failures = rows.filter(row => failureStatuses.has(row.status)).length;
    const metric = (title, value, foot, symbol, extra = '') => el('div', {class: `metric ${extra}`}, el('div', {class: 'metric-top'}, title, icon(symbol)), el('div', {class: 'metric-value'}, value), el('div', {class: 'metric-foot'}, foot));
    const metrics = el('div', {class: 'metrics'}, metric('Total deployments', rows.length, 'All deployment runs', 'grid'), metric('Ready to use', completed, 'Successfully provisioned', 'checkCircle', 'success'), metric('In progress', active, 'Queued, running, or cleaning', 'activity'), metric('Needs attention', failures, 'Review errors and recover', 'alert', failures ? 'warning' : ''));
    const search = el('input', {class: 'search-field', type: 'search', placeholder: 'Search deployments…', 'aria-label': 'Search deployments', value: state.search, onInput: event => { state.search = event.target.value; renderDeploymentRows(); }});
    const filter = el('select', {class: 'filter-select', 'aria-label': 'Filter by deployment status', onChange: event => { state.filter = event.target.value; renderDeploymentRows(); }}, el('option', {value: 'all'}, 'All statuses'), el('option', {value: 'active'}, 'In progress'), el('option', {value: 'completed'}, 'Completed'), el('option', {value: 'attention'}, 'Needs attention'), el('option', {value: 'reverted'}, 'Reverted'));
    filter.value = state.filter;
    const history = el('section', {class: 'surface', 'aria-labelledby': 'history-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'history-title'}, 'Deployment history', el('span', {class: 'count-badge'}, rows.length)), el('p', {}, 'Every deployment, from first boot to ready.')), rows.length ? el('div', {class: 'surface-header-actions'}, search, filter) : null), el('div', {id: 'deployment-rows'}));
    const setupBanner = !state.settings?.configured || !state.settings?.iso_configured ? el('div', {class: 'connection-banner'}, icon('server'), el('div', {}, el('strong', {}, 'Finish your GDeploy setup'), el('p', {}, 'Connect an ESXi host and select an OS ISO before creating a deployment.')), el('a', {href: state.settings?.configured ? '#settings/media' : '#settings/connection', class: 'button button-small'}, 'Open Setup', icon('arrow'))) : null;
    page.replaceChildren(...[heading('Deployments', 'Build your environment. We’ll take care of the setup.', button('New deployment', 'button-primary', openWizard, 'plus')), metrics, setupBanner, history].filter(Boolean));
    renderDeploymentRows();
  }
  function renderDeploymentRows() {
    const container = $('#deployment-rows');
    if (!container) return;
    if (!state.deployments.length) {
      container.replaceChildren(el('div', {class: 'empty-state'}, el('div', {class: 'empty-icon'}, icon('server')), el('h3', {}, 'Your next environment starts here'), el('p', {}, 'Choose your software and VM resources. GDeploy provisions your machines and gets everything talking.'), button('Create your first deployment', 'button-primary', openWizard, 'plus'), el('div', {class: 'empty-flow'}, el('span', {}, el('b', {}, '1'), 'Choose software'), el('span', {}, el('b', {}, '2'), 'Configure VMs'), el('span', {}, el('b', {}, '3'), 'Deploy'))));
      return;
    }
    const query = state.search.trim().toLowerCase();
    const rows = state.deployments.filter(row => (!query || String(row.name).toLowerCase().includes(query) || (row.vms || []).some(vm => String(vm.name).toLowerCase().includes(query))) && (state.filter === 'all' || (state.filter === 'active' ? busyStatuses.has(row.status) : state.filter === 'attention' ? failureStatuses.has(row.status) : row.status === state.filter)));
    if (!rows.length) { container.replaceChildren(el('div', {class: 'empty-state'}, el('h3', {}, 'No matching deployments'), el('p', {}, 'Try another name or status filter.'))); return; }
    const body = el('tbody');
    for (const row of rows) {
      const vmRows = row.vms || [];
      body.append(el('tr', {}, el('td', {}, el('a', {class: 'deployment-name', href: `#deployment/${encodeURIComponent(row.id)}`}, row.name), el('div', {class: 'subline'}, vmRows.map(vm => (roles[vm.role] || {short: vm.role}).short).join(' · ') || 'OS deployment')), el('td', {}, statusBadge(row.status)), el('td', {}, `${vmRows.length} ${vmRows.length === 1 ? 'VM' : 'VMs'}`, el('div', {class: 'subline'}, `${vmRows.reduce((n, vm) => n + Number(vm.cpu || 0), 0)} vCPU · ${vmRows.reduce((n, vm) => n + Number(vm.ram_gb || 0), 0)} GB RAM`)), el('td', {}, el('time', {datetime: row.created_at || ''}, date(row.created_at, true))), el('td', {}, el('a', {class: 'table-arrow', href: `#deployment/${encodeURIComponent(row.id)}`, 'aria-label': `View ${row.name}`}, icon('arrow')))));
    }
    container.replaceChildren(el('div', {class: 'table-scroll'}, el('table', {}, el('thead', {}, el('tr', {}, ...['Deployment', 'Status', 'Resources', 'Created', ''].map(text => el('th', {scope: 'col'}, text)))), body)));
  }
  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return 'Size unavailable';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    const index = bytes > 0 ? Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024))) : 0;
    return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: index ? 1 : 0}).format(bytes / (1024 ** index))} ${units[index]}`;
  }
  function uploadMedia(file, sha256, onProgress) {
    return new Promise((resolve, reject) => {
      if (!state.session || setupRequired()) { reject(new Error('Sign in with your configured administrator account before uploading media.')); return; }
      const requestSession = state.session;
      const xhr = new XMLHttpRequest();
      const finish = () => { if (state.mediaUpload === xhr) state.mediaUpload = null; };
      xhr.open('POST', `/api/settings/media/upload?filename=${encodeURIComponent(file.name)}&sha256=${encodeURIComponent(sha256)}`);
      xhr.withCredentials = true;
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('X-CSRF-Token', requestSession.csrf_token);
      xhr.upload.onprogress = event => onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
      xhr.upload.onload = () => onProgress(file.size, file.size);
      xhr.onload = () => {
        finish();
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* Error responses may not contain JSON. */ }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(data); return; }
        if (state.session === requestSession) {
          if (xhr.status === 401) showLogin('Your session has expired. Sign in to continue.');
          if (xhr.status === 403 && data?.detail?.code === 'credentials_change_required') showSetup({...requestSession, must_change_credentials: true}, data.detail.message);
        }
        reject(new Error(errorText(data, `Upload failed (${xhr.status}).`)));
      };
      xhr.onerror = () => { finish(); reject(new Error('The upload connection was interrupted. Check your connection and try again.')); };
      xhr.onabort = () => { finish(); reject(new Error('Upload canceled. Refresh the list to check the saved selection before retrying.')); };
      state.mediaUpload = xhr;
      xhr.send(file);
    });
  }
  function createMediaPanel(onChange, onBusy) {
    const viewEpoch = state.routeEpoch;
    let catalog = null, mode = 'server', busy = '', externalBusy = false;
    let esxiListing = null, browseBusy = false, browseRequest = 0;
    let browseTarget = {datastore: '', folder: ''};
    const currentView = () => panel.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const normalizedHost = value => String(value || '').trim().toLowerCase();
    const sourceLabel = item => ({server: 'GDeploy server', upload: 'Uploaded', esxi: 'ESXi copy'})[item.source] || 'GDeploy server';
    const originLabel = origin => origin ? `${origin.host} · [${origin.datastore}] ${origin.path}` : '';
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const successBox = el('div', {class: 'alert alert-success', role: 'status', hidden: true});
    const selectedSummary = el('div', {class: 'media-selected-summary'});
    const badge = el('span', {class: 'status'}, 'Loading…');
    const serverSelect = el('select', {id: 'os-media-server', name: 'media_id', required: true, 'aria-describedby': 'os-media-server-help'});
    const fileInput = el('input', {id: 'os-media-file', name: 'iso_file', type: 'file', accept: '.iso', 'aria-describedby': 'os-media-file-help'});
    const fileHelp = el('small', {id: 'os-media-file-help'}, 'Upload an .iso file from your computer.');
    const checksum = el('input', {id: 'os-media-sha256', name: 'sha256', type: 'text', required: true, minlength: 64, maxlength: 64, pattern: '[a-fA-F0-9]{64}', placeholder: 'Paste the publisher’s 64-character SHA-256 checksum', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false', class: 'mono', 'aria-describedby': 'os-media-sha256-help'});
    const sourceInput = value => el('input', {type: 'radio', name: 'media-source', value, checked: mode === value, onChange: () => {
      mode = value; browseRequest++; browseBusy = false;
      browseRefresh.replaceChildren(icon('refresh'), 'Refresh folder');
      checksum.value = value === 'server' ? currentItem()?.sha256 || '' : '';
      inlineError(errorBox, ''); successBox.hidden = true; syncControls();
      if (value === 'esxi') browseESXi(browseTarget.datastore, browseTarget.folder);
    }});
    const serverRadio = sourceInput('server'), uploadRadio = sourceInput('upload'), esxiRadio = sourceInput('esxi');
    const serverOrigin = el('p', {class: 'media-origin', hidden: true});
    const serverFields = el('div', {class: 'media-source-fields'}, field('OS ISO on the GDeploy server', serverSelect), serverOrigin, el('p', {class: 'media-help', id: 'os-media-server-help'}, 'Select a mounted ISO, a previous upload, or a saved ESXi copy. To add server files, place them in the media folder beside compose.yaml, then refresh this list.'));
    const uploadFields = el('div', {class: 'media-source-fields', hidden: true}, field('OS ISO file', fileInput), fileHelp);
    const datastoreSelect = el('select', {id: 'os-media-esxi-datastore', name: 'esxi_datastore', required: true}, el('option', {value: ''}, 'Choose a datastore'));
    const esxiFileSelect = el('select', {id: 'os-media-esxi-file', name: 'esxi_iso', required: true, 'aria-describedby': 'os-media-esxi-location'}, el('option', {value: ''}, 'Choose an ISO from this folder'));
    const folderPath = el('code', {id: 'os-media-esxi-folder'}, 'Datastore root');
    const folderList = el('div', {class: 'media-folders', 'aria-label': 'Datastore folders', role: 'group'});
    const browseStatus = el('p', {class: 'media-help', role: 'status', 'aria-live': 'polite'});
    const browseError = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const esxiLocation = el('p', {class: 'media-origin', id: 'os-media-esxi-location', hidden: true});
    const browseRefresh = button('Refresh folder', 'button-small', () => browseESXi(browseTarget.datastore, browseTarget.folder), 'refresh');
    browseRefresh.id = 'os-media-esxi-refresh';
    const up = button('Up one folder', 'button-ghost button-small', () => { const parent = parentFolder(); if (parent !== null) browseESXi(browseTarget.datastore, parent); }, 'back');
    up.id = 'os-media-esxi-up';
    const connectionPrompt = el('div', {class: 'alert alert-info'}, 'Save your ESXi connection before browsing its datastores. ', el('a', {href: '#settings/connection', class: 'media-connection-link'}, 'Open ESXi connection'));
    const esxiBrowser = el('div', {}, field('ESXi datastore', datastoreSelect),
      el('div', {class: 'media-browser-toolbar'}, el('div', {class: 'media-folder-location'}, el('span', {}, 'Current folder'), folderPath), el('div', {class: 'media-browser-actions'}, up, browseRefresh)),
      browseError, browseStatus, folderList, field('OS ISO in this folder', esxiFileSelect), esxiLocation);
    const esxiFields = el('div', {class: 'media-source-fields', id: 'os-media-esxi', hidden: true}, connectionPrompt, esxiBrowser,
      el('p', {class: 'media-help'}, 'GDeploy copies the selected ISO from ESXi and checks it before use. The original stays unchanged. Allow enough free space on the GDeploy server for the ISO copy and installation media.'));
    const progress = el('progress', {max: 100, value: 0, 'aria-label': 'OS ISO upload progress'});
    const progressText = el('span', {role: 'status', 'aria-live': 'polite'});
    const cancel = button('Cancel upload', 'button-ghost button-small', () => state.mediaUpload?.abort());
    const progressBox = el('div', {class: 'media-progress', hidden: true}, progress, el('div', {}, progressText, cancel));
    const currentItem = () => catalog?.items?.find(item => item.id === serverSelect.value);
    const currentEsxiFile = () => esxiListing?.files.find(file => file.path === esxiFileSelect.value);
    const currentEsxiHost = () => esxiListing && state.settings?.configured && normalizedHost(esxiListing.host) === normalizedHost(state.settings.host);
    function parentFolder() {
      if (esxiListing && esxiListing.datastore === browseTarget.datastore && esxiListing.folder === browseTarget.folder) return esxiListing.parent;
      if (!browseTarget.folder) return null;
      const separator = browseTarget.folder.lastIndexOf('/');
      return separator < 0 ? '' : browseTarget.folder.slice(0, separator);
    }
    function syncControls() {
      const locked = Boolean(busy || externalBusy || !catalog);
      for (const radio of [serverRadio, uploadRadio, esxiRadio]) { radio.checked = mode === radio.value; radio.disabled = locked; }
      serverFields.hidden = mode !== 'server'; uploadFields.hidden = mode !== 'upload'; esxiFields.hidden = mode !== 'esxi';
      serverSelect.disabled = locked || mode !== 'server';
      fileInput.disabled = locked || mode !== 'upload'; fileInput.required = mode === 'upload';
      const browsingDisabled = locked || mode !== 'esxi' || !state.settings?.configured || browseBusy;
      connectionPrompt.hidden = Boolean(state.settings?.configured); esxiBrowser.hidden = !state.settings?.configured;
      datastoreSelect.disabled = browsingDisabled || datastoreSelect.options.length <= 1;
      esxiFileSelect.disabled = browsingDisabled || !currentEsxiHost();
      browseRefresh.disabled = browsingDisabled;
      up.disabled = browsingDisabled || parentFolder() === null;
      for (const folder of folderList.querySelectorAll('button')) folder.disabled = browsingDisabled;
      checksum.disabled = locked || (mode === 'esxi' && (!state.settings?.configured || browseBusy));
      refresh.disabled = Boolean(busy || externalBusy);
      refresh.hidden = mode === 'esxi';
      submit.disabled = locked || (mode === 'server' && !serverSelect.value) || (mode === 'upload' && !fileInput.files.length) || (mode === 'esxi' && (browseBusy || !currentEsxiHost() || !currentEsxiFile()));
      if (!busy) submit.replaceChildren(icon(mode === 'server' ? 'check' : 'disc'), mode === 'upload' ? 'Upload & use ISO' : mode === 'esxi' ? 'Copy & use ISO' : 'Use selected ISO');
      const serverItem = currentItem();
      serverOrigin.hidden = !serverItem?.origin;
      serverOrigin.textContent = serverItem?.origin ? `ESXi copy from ${originLabel(serverItem.origin)}` : '';
      esxiLocation.hidden = !currentEsxiFile();
      esxiLocation.textContent = currentEsxiFile() ? `${esxiListing.host} · [${esxiListing.datastore}] ${esxiFileSelect.value}` : '';
      esxiBrowser.setAttribute('aria-busy', browseBusy ? 'true' : 'false');
      panel.setAttribute('aria-busy', busy ? 'true' : 'false');
    }
    async function browseESXi(datastore = '', folder = '') {
      if (!currentView() || mode !== 'esxi' || busy || externalBusy) return;
      const request = ++browseRequest, requestedHost = normalizedHost(state.settings?.host);
      if (!state.settings?.configured) { esxiListing = null; browseBusy = false; syncControls(); return; }
      const previousListing = esxiListing, previousFile = esxiFileSelect.value, previousChecksum = checksum.value;
      browseTarget = {datastore, folder};
      folderPath.textContent = folder || 'Datastore root';
      browseBusy = true; successBox.hidden = true; inlineError(browseError, '');
      browseStatus.textContent = `Loading folders and ISO files from ${state.settings.host}…`;
      setBusy(browseRefresh, 'Loading…'); syncControls();
      const currentRequest = () => currentView() && mode === 'esxi' && request === browseRequest && requestedHost === normalizedHost(state.settings?.host);
      try {
        const query = new URLSearchParams({folder});
        if (datastore) query.set('datastore', datastore);
        const listing = await api(`/api/settings/media/esxi?${query}`);
        if (!currentRequest()) return;
        if (normalizedHost(listing.host) !== requestedHost) throw new Error('The saved ESXi host changed. Reload Setup before choosing an ISO.');
        esxiListing = listing; browseTarget = {datastore: listing.datastore, folder: listing.folder};
        datastoreSelect.replaceChildren(el('option', {value: ''}, 'Choose a datastore'), ...listing.datastores.map(store => el('option', {value: store.name}, `${store.name} · ${Math.floor(Number(store.free_gb) || 0)} GB free`)));
        datastoreSelect.value = listing.datastore;
        folderPath.textContent = listing.folder || 'Datastore root';
        folderList.replaceChildren(...listing.folders.map(folderItem => el('button', {type: 'button', class: 'media-folder', 'aria-label': `Open folder ${folderItem.name}`, onClick: () => browseESXi(listing.datastore, folderItem.path)}, icon('folder'), el('span', {}, folderItem.name), icon('arrow'))));
        esxiFileSelect.replaceChildren(el('option', {value: ''}, listing.files.length ? 'Choose an OS ISO' : 'No ISO files in this folder'), ...listing.files.map(file => el('option', {value: file.path}, `${file.name} · ${formatBytes(file.size_bytes)}`)));
        const keepSelection = previousListing?.host === listing.host && previousListing?.datastore === listing.datastore && previousListing?.folder === listing.folder && listing.files.some(file => file.path === previousFile);
        esxiFileSelect.value = keepSelection ? previousFile : '';
        checksum.value = keepSelection ? previousChecksum : '';
        browseStatus.textContent = listing.folders.length || listing.files.length ? `${listing.folders.length} ${listing.folders.length === 1 ? 'folder' : 'folders'} · ${listing.files.length} ${listing.files.length === 1 ? 'ISO file' : 'ISO files'}` : 'This folder contains no subfolders or ISO files.';
      } catch (error) {
        if (!currentRequest()) return;
        esxiListing = null; esxiFileSelect.replaceChildren(el('option', {value: ''}, 'Refresh the folder list to choose an ISO')); folderList.replaceChildren();
        checksum.value = ''; inlineError(browseError, error.message); browseStatus.textContent = 'Check the ESXi connection, datastore permissions, and folder, then refresh.';
      } finally {
        if (currentRequest()) { browseBusy = false; browseRefresh.replaceChildren(icon('refresh'), 'Refresh folder'); syncControls(); }
      }
    }
    function renderCatalog(next) {
      catalog = next;
      const previousId = serverSelect.value;
      serverSelect.replaceChildren(el('option', {value: ''}, next.items.length ? 'Choose an OS ISO' : 'No ISOs found on this server'), ...next.items.map(item => el('option', {value: item.id}, `${item.name} · ${formatBytes(item.size_bytes)} · ${sourceLabel(item)}`)));
      serverSelect.value = next.selected?.id || (next.items.some(item => item.id === previousId) ? previousId : '');
      if (mode === 'server') checksum.value = currentItem()?.sha256 || '';
      fileHelp.textContent = `Upload an .iso file up to ${formatBytes(next.max_upload_bytes)}. Uploads are kept with GDeploy’s persistent data.`;
      badge.className = next.ready ? 'status status-completed' : 'status';
      badge.textContent = next.ready ? 'Ready for preflight' : 'Not configured';
      if (next.selected) {
        selectedSummary.replaceChildren(el('div', {class: 'media-summary-heading'}, icon('disc'), el('div', {}, el('span', {class: 'eyebrow'}, 'SAVED OS ISO'), el('strong', {}, next.selected.name), el('p', {}, `${formatBytes(next.selected.size_bytes)} · ${sourceLabel(next.selected)}`))), el('details', {class: 'media-integrity'}, el('summary', {}, 'Saved SHA-256 checksum'), el('code', {class: 'certificate-fingerprint'}, next.selected.sha256)));
        if (next.selected.origin) selectedSummary.append(el('p', {class: 'media-origin'}, el('strong', {}, 'Copied from ESXi'), el('span', {}, originLabel(next.selected.origin))));
        if (!next.ready) selectedSummary.append(el('p', {class: 'media-help'}, 'The saved media is unavailable or needs attention. Choose a valid ISO below before deploying.'));
      } else selectedSummary.replaceChildren(el('div', {class: 'media-summary-heading'}, icon('disc'), el('div', {}, el('strong', {}, 'Choose your installation media'), el('p', {}, 'This OS ISO will be used for new deployments.'))));
      onChange(next);
    }
    async function load() {
      if (!currentView() || busy || externalBusy) return;
      busy = 'load'; inlineError(errorBox, ''); syncControls(); setBusy(refresh, 'Refreshing…');
      try {
        const next = await api('/api/settings/media');
        if (!currentView()) return;
        if (!catalog && !next.items.length) mode = 'upload';
        renderCatalog(next);
      } catch (error) { if (currentView()) { inlineError(errorBox, error.message); badge.textContent = 'Couldn’t load media'; } }
      finally { if (currentView()) { busy = ''; refresh.replaceChildren(icon('refresh'), 'Refresh list'); syncControls(); } }
    }
    const refresh = button('Refresh list', 'button-small', load, 'refresh');
    const submit = button('Use selected ISO', 'button-primary', null, 'check'); submit.type = 'submit';
    const form = el('form', {onSubmit: async event => {
      event.preventDefault();
      if (!currentView() || busy || externalBusy || !catalog || !form.reportValidity()) return;
      const file = fileInput.files[0];
      if (mode === 'upload' && (!file || !file.name.toLowerCase().endsWith('.iso') || !file.size || file.size > catalog.max_upload_bytes)) {
        inlineError(errorBox, `Choose a nonempty .iso file no larger than ${formatBytes(catalog.max_upload_bytes)}.`); return;
      }
      const uploading = mode === 'upload', importing = mode === 'esxi';
      if (importing && (browseBusy || !currentEsxiHost() || !currentEsxiFile())) { inlineError(errorBox, 'Refresh the ESXi folder list and choose an ISO before copying it.'); return; }
      const esxiSource = importing ? {host: esxiListing.host, datastore: esxiListing.datastore, path: esxiFileSelect.value} : null;
      busy = uploading ? 'upload' : importing ? 'import' : 'save'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true;
      progressBox.hidden = !uploading && !importing; cancel.hidden = !uploading;
      progress.setAttribute('aria-label', importing ? 'OS ISO copy progress' : 'OS ISO upload progress');
      if (uploading) { progress.value = 0; cancel.disabled = false; progressText.textContent = 'Starting upload…'; }
      if (importing) { progress.removeAttribute('value'); progressText.textContent = 'Copying & verifying ISO… This may take several minutes.'; }
      setBusy(submit, uploading ? 'Uploading ISO…' : importing ? 'Copying & verifying ISO…' : 'Verifying ISO…'); syncControls();
      try {
        const digest = checksum.value.trim().toLowerCase();
        const next = uploading ? await uploadMedia(file, digest, (loaded, total) => {
          if (!currentView()) return;
          if (loaded >= total) { progress.removeAttribute('value'); cancel.disabled = true; progressText.textContent = 'Upload complete. Verifying checksum and installation media…'; setBusy(submit, 'Verifying ISO…'); }
          else { progress.value = Math.round(loaded / total * 100); progressText.textContent = `${formatBytes(loaded)} of ${formatBytes(total)} uploaded`; }
        }) : importing ? await api('/api/settings/media/esxi', {method: 'POST', body: {...esxiSource, sha256: digest}}) : await api('/api/settings/media', {method: 'PUT', body: {media_id: serverSelect.value, sha256: digest}});
        if (!currentView()) return;
        mode = 'server'; fileInput.value = ''; renderCatalog(next);
        successBox.textContent = importing ? `${next.selected?.name || 'OS ISO'} copied from ESXi, verified, and saved. New deployments will use this copy.` : `${next.selected?.name || 'OS ISO'} verified and saved. New deployments will use this media.`;
        successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { busy = ''; progressBox.hidden = true; onBusy(false); syncControls(); } }
    }}, el('div', {class: 'form-body'}, selectedSummary, errorBox, successBox,
      el('fieldset', {class: 'media-source-options'}, el('legend', {}, 'Choose an ISO source'), el('label', {}, serverRadio, 'GDeploy server'), el('label', {}, esxiRadio, 'ESXi datastore'), el('label', {}, uploadRadio, 'Upload an ISO')),
      serverFields, esxiFields, uploadFields,
      field('Publisher SHA-256 checksum', checksum, el('span', {id: 'os-media-sha256-help'}, 'Copy the checksum from the OS publisher’s download page. GDeploy verifies the file before saving it.')),
      el('p', {class: 'media-compatibility'}, icon('info'), 'Automatic installation currently supports Ubuntu Server 24.04 LTS amd64 using autoinstall. Other ISOs are not supported.'), progressBox), el('div', {class: 'form-footer'}, el('span', {class: 'media-footer-note'}, 'Saved changes apply to new deployments.'), submit));
    const panel = el('section', {class: 'surface', id: 'os-media-panel', 'aria-labelledby': 'os-media-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'os-media-title'}, 'OS installation media'), el('p', {}, 'Choose and verify the ISO used to install your virtual machines.')), badge), form);
    $('.surface-header', panel).append(refresh);
    serverSelect.addEventListener('change', () => { checksum.value = currentItem()?.sha256 || ''; successBox.hidden = true; syncControls(); });
    fileInput.addEventListener('change', () => { checksum.value = ''; successBox.hidden = true; inlineError(errorBox, ''); syncControls(); });
    datastoreSelect.addEventListener('change', () => browseESXi(datastoreSelect.value, ''));
    esxiFileSelect.addEventListener('change', () => { checksum.value = ''; successBox.hidden = true; inlineError(errorBox, ''); syncControls(); });
    checksum.addEventListener('input', () => { checksum.value = checksum.value.trim(); });
    return {panel, load, setExternalBusy(value) { externalBusy = value; syncControls(); }};
  }
  function renderSettings() {
    const settings = state.settings || {};
    const viewEpoch = state.routeEpoch;
    const hostKey = value => String(value || '').trim().toLowerCase();
    const host = el('input', {id: 'esxi-host', name: 'host', required: true, maxlength: 253, value: settings.host || '', placeholder: 'esxi.example.com', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'esxi-host-help'});
    const username = el('input', {id: 'esxi-username', name: 'username', required: true, value: settings.username || '', placeholder: 'Your ESXi service account', autocomplete: 'off', spellcheck: 'false'});
    const password = el('input', {id: 'esxi-password', name: 'password', type: 'password', required: !settings.configured, placeholder: settings.configured ? 'Leave blank to keep the saved password' : 'ESXi account password', autocomplete: 'new-password'});
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const resultBox = el('div', {hidden: true});
    const certificateError = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const certificateStatus = el('p', {class: 'certificate-status-text', role: 'status', 'aria-live': 'polite'});
    const certificateDetails = el('div', {id: 'esxi-certificate-details'});
    let certificate = settings.certificate_trust || null;
    let certificateHost = hostKey(host.value);
    let certificateSource = 'saved';
    let certificateRequest = 0;
    let certificateBusy = '';
    let connectionBusy = '';
    let mediaBusy = false;
    let mediaControls = null;
    let fingerprintConfirmed = false;
    let trustButton = null;
    let removeButton = null;
    let comparison = null;
    const currentView = () => form.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const currentCertificate = () => certificate && certificateHost === hostKey(host.value);
    const certificateUsable = () => {
      if (!currentCertificate() || certificate.can_trust !== true || !certificate.fingerprint_sha256) return false;
      const beginning = Date.parse(certificate.valid_from), ending = Date.parse(certificate.valid_until);
      return (!Number.isFinite(beginning) || beginning <= Date.now()) && (!Number.isFinite(ending) || ending > Date.now());
    };
    const certificateDate = value => {
      const parsed = new Date(value);
      return value && !Number.isNaN(parsed.getTime()) ? new Intl.DateTimeFormat(undefined, {year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'}).format(parsed) : 'Not available';
    };
    function syncControls() {
      const busy = Boolean(connectionBusy || certificateBusy || mediaBusy);
      const identityChanged = hostKey(host.value) !== hostKey(settings.host) || username.value.trim() !== (settings.username || '');
      const lockInputs = Boolean(connectionBusy || ['trust', 'remove'].includes(certificateBusy));
      host.disabled = lockInputs; username.disabled = lockInputs; password.disabled = lockInputs;
      password.required = !settings.configured || identityChanged;
      save.disabled = busy;
      test.disabled = busy || !settings.configured || identityChanged || Boolean(password.value);
      retrieve.disabled = busy || !host.value.trim();
      if (trustButton) trustButton.disabled = busy || !fingerprintConfirmed || !certificateUsable();
      if (removeButton) removeButton.disabled = busy;
      if (comparison) comparison.disabled = busy || !certificateUsable();
      certificatePanel.setAttribute('aria-busy', certificateBusy ? 'true' : 'false');
      mediaControls?.setExternalBusy(Boolean(connectionBusy || certificateBusy));
    }
    function renderCertificate() {
      trustButton = null; removeButton = null; comparison = null;
      if (!currentCertificate()) {
        certificateDetails.replaceChildren(el('p', {class: 'certificate-empty'}, 'Retrieve the certificate using only the host address. No ESXi username or password is sent.'));
        return;
      }
      const cert = certificate;
      const trusted = cert.trusted === true;
      const changed = Boolean(cert.trusted_fingerprint_sha256 && cert.trusted_fingerprint_sha256 !== cert.fingerprint_sha256);
      const detail = (label, value, full = false) => el('div', {class: full ? 'certificate-detail-wide' : ''}, el('dt', {}, label), el('dd', {}, value));
      const names = values => Array.isArray(values) && values.length ? values.join(', ') : 'None listed';
      const metadata = el('dl', {class: 'certificate-metadata'},
        detail('SHA-256 fingerprint', el('code', {class: 'certificate-fingerprint', tabindex: '0', 'aria-label': 'Certificate SHA-256 fingerprint'}, cert.fingerprint_sha256 || 'Not available'), true),
        detail('Subject', cert.subject || 'Not available', true), detail('Issuer', cert.issuer || 'Not available', true),
        detail('Valid from', certificateDate(cert.valid_from)), detail('Valid until', certificateDate(cert.valid_until)),
        detail('DNS names', names(cert.dns_names)), detail('IP addresses', names(cert.ip_addresses)));
      if (trusted && cert.trusted_at) metadata.append(detail('Trust saved', certificateDate(cert.trusted_at), true));
      const content = [el('div', {class: 'certificate-summary'}, el('span', {class: trusted ? 'status status-completed' : 'status'}, trusted ? 'Certificate trusted' : 'Trust not saved'), el('span', {class: 'certificate-endpoint'}, cert.endpoint || cert.host || host.value.trim())), metadata];
      if (certificateSource === 'saved') content.push(el('p', {class: 'certificate-help'}, 'Showing the saved certificate. Retrieve it again to compare it with the certificate currently presented by this host.'));
      if (changed) content.push(el('div', {class: 'alert alert-warning'}, el('strong', {}, 'This host is presenting a different certificate. '), 'Compare the new fingerprint with ESXi before replacing the saved trust.', el('div', {class: 'certificate-previous'}, 'Previously trusted SHA-256 fingerprint', el('code', {class: 'certificate-fingerprint'}, cert.trusted_fingerprint_sha256))));
      if (!certificateUsable()) content.push(el('div', {class: 'alert alert-warning'}, cert.validation_error || 'This certificate is expired, not yet valid, or cannot be trusted. Correct the certificate on ESXi, then retrieve it again.'));
      if (!trusted && certificateUsable()) {
        comparison = el('input', {type: 'checkbox', id: 'certificate-fingerprint-confirmed', checked: fingerprintConfirmed, onChange: event => { fingerprintConfirmed = event.target.checked; syncControls(); }});
        content.push(el('label', {class: 'certificate-comparison', for: 'certificate-fingerprint-confirmed'}, comparison, el('span', {}, 'I compared this SHA-256 fingerprint with the certificate shown by ESXi or another trusted source.')));
        trustButton = button('Trust certificate', 'button-primary', async () => {
          if (!currentView() || certificateBusy || connectionBusy || !fingerprintConfirmed || !certificateUsable() || certificate !== cert) return;
          const requestedHost = host.value.trim(), request = ++certificateRequest;
          certificateBusy = 'trust'; fingerprintConfirmed = false;
          inlineError(certificateError, ''); certificateStatus.textContent = 'Checking the presented certificate and saving trust…';
          setBusy(trustButton, 'Saving trust…'); syncControls();
          try {
            const result = await api('/api/settings/certificate/trust', {method: 'POST', body: {host: requestedHost, fingerprint_sha256: cert.fingerprint_sha256}});
            if (!currentView() || request !== certificateRequest || hostKey(requestedHost) !== hostKey(host.value)) return;
            certificate = result; certificateHost = hostKey(requestedHost); certificateSource = 'retrieved';
            if (state.settings && hostKey(state.settings.host) === certificateHost) state.settings.certificate_trust = result;
            state.inventory = null;
            renderReadiness();
            certificateStatus.textContent = 'Certificate trusted for this host. Save the connection details, then test the saved connection. No restart is needed.';
          } catch (error) {
            if (!currentView() || request !== certificateRequest) return;
            certificate = null;
            inlineError(certificateError, error.message);
            certificateStatus.textContent = 'Trust was not saved. Retrieve the certificate again before reviewing its fingerprint.';
          } finally {
            if (currentView() && request === certificateRequest) { certificateBusy = ''; renderCertificate(); syncControls(); }
          }
        }, 'shield');
      }
      if (trusted || cert.trusted_fingerprint_sha256) {
        removeButton = button('Remove trust', 'button-ghost', async () => {
          if (!currentView() || certificateBusy || connectionBusy || certificate !== cert || !currentCertificate()) return;
          const requestedHost = host.value.trim(), request = ++certificateRequest;
          certificateBusy = 'remove'; fingerprintConfirmed = false;
          inlineError(certificateError, ''); certificateStatus.textContent = 'Removing saved trust for this host…';
          setBusy(removeButton, 'Removing trust…'); syncControls();
          try {
            await api('/api/settings/certificate', {method: 'DELETE', body: {host: requestedHost}});
            if (!currentView() || request !== certificateRequest || hostKey(requestedHost) !== hostKey(host.value)) return;
            certificate = null;
            if (state.settings && hostKey(state.settings.host) === hostKey(requestedHost)) state.settings.certificate_trust = null;
            state.inventory = null;
            renderReadiness();
            certificateStatus.textContent = 'Saved trust removed. Standard certificate authority verification remains enabled. Retrieve the certificate to review trust again.';
          } catch (error) {
            if (currentView() && request === certificateRequest) { inlineError(certificateError, error.message); certificateStatus.textContent = 'Saved trust could not be removed.'; }
          } finally {
            if (currentView() && request === certificateRequest) { certificateBusy = ''; renderCertificate(); syncControls(); }
          }
        });
      }
      if (trustButton || removeButton) content.push(el('div', {class: 'certificate-actions'}, trustButton, removeButton));
      certificateDetails.replaceChildren(...content);
    }
    const retrieve = button('Retrieve certificate', 'button-small', async () => {
      if (!currentView() || connectionBusy || certificateBusy || !host.reportValidity()) return;
      const requestedHost = host.value.trim(), request = ++certificateRequest;
      certificateBusy = 'inspect'; fingerprintConfirmed = false;
      inlineError(certificateError, ''); resultBox.hidden = true;
      certificateStatus.textContent = `Retrieving the certificate from ${requestedHost}…`;
      setBusy(retrieve, 'Retrieving…'); syncControls();
      try {
        const result = await api('/api/settings/certificate/inspect', {method: 'POST', body: {host: requestedHost}});
        if (!currentView() || request !== certificateRequest || hostKey(requestedHost) !== hostKey(host.value)) return;
        certificate = result; certificateHost = hostKey(requestedHost); certificateSource = 'retrieved';
        certificateStatus.textContent = result.trusted ? 'The presented certificate matches the saved trust for this host.' : 'Certificate retrieved. Compare its fingerprint with a trusted source before accepting it.';
      } catch (error) {
        if (currentView() && request === certificateRequest) { certificate = null; inlineError(certificateError, error.message); certificateStatus.textContent = 'The certificate could not be retrieved. Check the host address and HTTPS access on port 443.'; }
      } finally {
        if (currentView() && request === certificateRequest) {
          certificateBusy = ''; retrieve.replaceChildren(icon('refresh'), 'Retrieve certificate'); renderCertificate(); syncControls();
        }
      }
    }, 'refresh');
    const certificatePanel = el('section', {class: 'certificate-panel', 'aria-labelledby': 'certificate-title', 'aria-busy': 'false'},
      el('div', {class: 'certificate-heading'}, el('div', {}, el('h3', {id: 'certificate-title'}, icon('shield'), 'ESXi certificate'), el('p', {}, 'Review the host’s identity before trusting its connection.')), retrieve),
      certificateError, certificateStatus, certificateDetails,
      el('p', {class: 'certificate-help certificate-explanation'}, 'Trust is limited to this host and its exact certificate fingerprint. It supports self-signed certificates and IP/name mismatches. A changed certificate must be reviewed again; no container restart is needed.'));
    const save = button('Save connection', 'button-primary', null, 'check'); save.type = 'submit';
    const test = button('Test saved connection', '', async () => {
      if (!currentView() || test.disabled) return;
      inlineError(errorBox, ''); resultBox.hidden = true; connectionBusy = 'test'; setBusy(test, 'Connecting…'); syncControls();
      state.inventory = null; renderReadiness();
      try {
        const inventory = await api('/api/inventory');
        if (!currentView()) return;
        state.inventory = inventory;
        renderReadiness();
        resultBox.className = 'alert alert-success';
        resultBox.replaceChildren(el('strong', {}, `Connected to ${inventory.host?.name || settings.host}`), el('div', {class: 'inventory-summary'}, el('span', {}, `${inventory.host?.cpu_threads || 0} CPU threads`), el('span', {}, `${inventory.host?.memory_gb || 0} GB memory`), el('span', {}, `${inventory.datastores?.length || 0} datastores`), el('span', {}, `${inventory.networks?.length || 0} networks`)));
        resultBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { connectionBusy = ''; test.replaceChildren(icon('refresh'), 'Test saved connection'); syncControls(); } }
    }, 'refresh');
    const form = el('form', {class: 'surface', id: 'esxi-connection-panel', onSubmit: async event => {
      event.preventDefault();
      if (!currentView() || connectionBusy || certificateBusy || mediaBusy || !form.reportValidity()) return;
      const payload = {host: host.value.trim(), username: username.value.trim(), password: password.value, verify_tls: true};
      inlineError(errorBox, ''); resultBox.hidden = true; connectionBusy = 'save'; setBusy(save, 'Saving…'); syncControls();
      try {
        await api('/api/settings', {method: 'PUT', body: payload});
        if (!currentView()) return;
        password.value = '';
        const updated = await api('/api/settings');
        if (!currentView()) return;
        state.settings = updated; state.inventory = null;
        renderSettings();
        notify('ESXi connection saved. Test the connection to check access.');
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { connectionBusy = ''; save.replaceChildren(icon('check'), 'Save connection'); syncControls(); } }
    }}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {}, 'ESXi connection'), el('p', {}, 'Review the certificate, then save the account used to manage your VMs.')), settings.configured ? el('span', {class: 'status status-completed'}, 'Configured') : el('span', {class: 'status'}, 'Not configured')), el('div', {class: 'form-body'}, errorBox, resultBox, field('ESXi host', host, el('span', {id: 'esxi-host-help'}, 'Use a hostname or IP address, without a URL or port. ESXi HTTPS uses port 443.')), certificatePanel, field('Username', username, 'Use an account with the required VM, network, and datastore permissions.'), field('Password', password, settings.configured ? 'Changing the host or username requires entering its password again.' : 'Your saved password is never returned by the settings API.'), el('div', {class: 'secure-note'}, icon('shield'), el('div', {}, el('strong', {}, 'Certificate verification stays enabled'), 'Use the trusted certificate above or the certificate authorities configured on the GDeploy server.'))), el('div', {class: 'form-footer'}, test, save));
    host.addEventListener('input', () => {
      certificateRequest++; certificateBusy = ''; certificate = null; fingerprintConfirmed = false;
      retrieve.replaceChildren(icon('refresh'), 'Retrieve certificate');
      inlineError(certificateError, ''); inlineError(errorBox, ''); resultBox.hidden = true;
      certificateStatus.textContent = host.value.trim() ? 'Host changed. Retrieve its certificate to review the current identity and saved trust.' : '';
      renderCertificate(); syncControls();
    });
    for (const input of [username, password]) input.addEventListener('input', () => { resultBox.hidden = true; syncControls(); });
    const item = (ready, title, description, symbol) => el('div', {class: `setup-item ${ready ? 'ready' : ''}`}, icon(ready ? 'checkCircle' : symbol), el('div', {}, el('strong', {}, title), el('p', {}, description)));
    const readiness = el('section', {class: 'surface', 'aria-label': 'Setup checklist'});
    const steps = el('div', {class: 'setup-steps', role: 'tablist', 'aria-label': 'Setup sections'});
    const tabs = {};
    const panels = {};
    function selectSetupTab(key, {focus = false, updateHash = true} = {}) {
      state.setupTab = key;
      for (const [name, tab] of Object.entries(tabs)) {
        const selected = name === key;
        tab.button.classList.toggle('active', selected);
        tab.button.setAttribute('aria-selected', String(selected));
        tab.button.tabIndex = selected ? 0 : -1;
        panels[name].hidden = !selected;
      }
      if (updateHash) history.replaceState(history.state, '', `#settings/${key}`);
      if (focus) tabs[key].button.focus();
    }
    function createSetupTab(key, number, title) {
      const marker = el('span', {class: 'setup-step-number', 'aria-hidden': 'true'}, number);
      const description = el('small');
      const tab = el('button', {type: 'button', class: 'setup-step', id: `setup-tab-${key}`, role: 'tab', 'aria-controls': `setup-panel-${key}`, 'aria-selected': 'false', tabindex: '-1', onClick: () => selectSetupTab(key), onKeydown: event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const names = ['connection', 'media'];
        const next = event.key === 'Home' ? names[0] : event.key === 'End' ? names[1] : names[(names.indexOf(key) + 1) % names.length];
        selectSetupTab(next, {focus: true});
      }}, marker, el('span', {}, el('strong', {}, title), description));
      tabs[key] = {button: tab, marker, description, number};
      steps.append(tab);
    }
    createSetupTab('connection', '1', 'ESXi connection');
    createSetupTab('media', '2', 'OS installation media');
    function renderReadiness() {
      const saved = state.settings || settings;
      const updateTab = (key, ready, description) => {
        const tab = tabs[key];
        tab.button.classList.toggle('ready', Boolean(ready));
        tab.marker.replaceChildren(ready ? icon('check') : tab.number);
        tab.description.textContent = description;
      };
      updateTab('connection', saved.configured, saved.configured ? state.inventory ? 'Connected to your host' : 'Connection saved · test access' : 'Connect your virtualization host');
      updateTab('media', saved.iso_configured, saved.iso_configured ? 'OS ISO selected · ready for preflight' : 'Select or upload an OS ISO');
      readiness.replaceChildren(el('div', {class: 'surface-header'}, el('h2', {}, 'Setup checklist')), el('div', {class: 'setup-list'}, item(saved.configured, 'ESXi host', saved.configured ? state.inventory ? 'Connection tested successfully.' : 'Connection details saved. Test the connection to verify access.' : 'Save your host credentials to load inventory.', 'server'), item(saved.iso_configured, 'OS ISO', saved.iso_configured ? 'Installation media saved. Preflight checks it again before deployment.' : 'Open the OS installation media tab to select or upload your ISO.', 'disc'), item(saved.splunk_configured, 'Splunk package · optional', saved.splunk_configured ? 'Splunk package configured. Its license must be accepted for each deployment.' : 'Only needed for Splunk: configure its Linux x86_64 .tgz package and checksum on the server.', 'layers')));
    }
    mediaControls = createMediaPanel(catalog => { if (state.settings) state.settings.iso_configured = catalog.ready; renderReadiness(); }, value => { mediaBusy = value; syncControls(); });
    panels.connection = el('div', {id: 'setup-panel-connection', role: 'tabpanel', 'aria-labelledby': 'setup-tab-connection', tabindex: '0'}, form);
    panels.media = el('div', {id: 'setup-panel-media', role: 'tabpanel', 'aria-labelledby': 'setup-tab-media', tabindex: '0'}, mediaControls.panel);
    page.replaceChildren(heading('Setup', 'Choose a section below to connect your ESXi host or select an OS ISO.'), steps, el('div', {class: 'settings-grid'}, el('div', {class: 'settings-main'}, panels.connection, panels.media), el('aside', {}, readiness, el('p', {class: 'settings-note'}, 'Elasticsearch and Kibana are installed from Elastic’s package repository; guests need outbound network access.'))));
    selectSetupTab(state.setupTab || (settings.configured && !settings.iso_configured ? 'media' : 'connection'), {updateHash: false});
    renderReadiness(); renderCertificate(); syncControls(); mediaControls.load();
  }
  function field(label, input, hint) { return el('label', {class: 'field'}, el('span', {}, label), input, hint ? el('small', {}, hint) : null); }
  function renderDetail(data) {
    const vms = data.vms || [];
    const back = el('a', {class: 'back-link', href: '#deployments'}, icon('back'), 'All deployments');
    const title = el('div', {class: 'page-heading'}, el('div', {}, el('div', {class: 'detail-title'}, el('h1', {}, data.name), statusBadge(data.status)), el('p', {class: 'detail-meta'}, `${vms.length} ${vms.length === 1 ? 'virtual machine' : 'virtual machines'} · Created ${date(data.created_at, true)}`)), button('Refresh', 'button-ghost button-small', () => refreshDetail(true), 'refresh'));
    const vmSurface = el('section', {class: 'surface'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Virtual machines', el('span', {class: 'count-badge'}, vms.length))));
    for (const vm of vms) {
      const spec = (label, value) => el('div', {}, el('dt', {}, label), el('dd', {}, value || '—'));
      vmSurface.append(el('article', {class: 'vm-card'}, el('div', {class: 'vm-card-head'}, el('div', {class: 'vm-card-name'}, roleIcon(vm.role), el('div', {}, el('h3', {}, vm.name), el('p', {}, roleName(vm.role)))), vm.status ? statusBadge(vm.status) : null), el('dl', {class: 'vm-specs'}, spec('CPU', `${vm.cpu} vCPU`), spec('Memory', `${vm.ram_gb} GB`), spec('Storage', `${vm.disk_gb} GB`), spec('Datastore', vm.datastore), spec('Network', vm.network), spec('IP address', vm.ip || (vm.ip_mode === 'static' ? vm.address : 'DHCP · pending'))), vm.services?.length ? el('div', {class: 'service-links'}, vm.services.map(serviceLink)) : null));
    }
    if (!vms.length) vmSurface.append(el('p', {class: 'no-events'}, 'VM details will appear as provisioning starts.'));
    const logList = el('ol', {class: 'log-list', 'aria-label': 'Deployment events'});
    for (const event of data.events || []) {
      const severity = ['error', 'warning'].includes(event.level) ? event.level : '';
      logList.append(el('li', {class: `log-item ${severity}`}, el('time', {datetime: event.at || '', title: date(event.at, true)}, eventTime(event.at)), el('span', {class: 'log-message'}, event.message)));
    }
    const logs = el('section', {class: 'surface'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Activity log'), el('span', {class: 'muted small'}, busyStatuses.has(data.status) ? 'Updates automatically' : `${data.events?.length || 0} events`)), data.events?.length ? logList : el('p', {class: 'no-events'}, 'No events recorded yet.'));
    const left = el('div', {class: 'detail-main'}, data.error ? el('div', {class: 'alert alert-error', role: 'alert'}, el('strong', {}, 'Deployment needs attention\n'), data.error) : null, vmSurface, logs);
    const credentials = renderCredentials();
    const aside = el('aside', {class: 'detail-aside'}, renderProgress(data), credentials);
    if (failureStatuses.has(data.status)) aside.append(el('section', {class: 'surface recovery-card'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Recover this deployment')), el('div', {class: 'recovery-body'}, el('p', {}, 'Delete the VMs and installation media owned by this failed deployment, then start again with the same VM configuration and the current OS ISO selection in Setup. All data on those VM disks will be deleted.'), button(data.status === 'cleanup_failed' ? 'Retry cleanup & redeploy' : 'Delete & redeploy', 'button-danger button-full', () => openRedeploy(data), 'refresh'))));
    page.replaceChildren(back, title, el('div', {class: 'detail-layout'}, left, aside));
  }
  function renderProgress(data) {
    const index = stageOrder.indexOf(data.stage);
    const completed = data.status === 'completed';
    const failed = failureStatuses.has(data.status);
    const percentage = completed ? 100 : index > 0 ? Math.round(index / (stageOrder.length - 1) * 100) : 0;
    const list = el('ol', {class: 'stage-list'});
    const stages = [['preflight', 'Preflight checks'], ['preparing', 'Prepare installation media'], ['creating', 'Create virtual machines'], ['installing_os', 'Install operating system'], ['installing_software', 'Install selected software'], ['verifying', 'Verify services']];
    for (const [key, label] of stages) {
      const stageIndex = stageOrder.indexOf(key);
      const done = completed || (index >= 0 && stageIndex < index);
      const current = stageIndex === index;
      list.append(el('li', {class: `${done ? 'done' : current ? 'current' : ''} ${failed ? 'failed' : ''}`}, el('span', {class: 'stage-marker'}, done ? icon('check') : current ? '•' : String(stageIndex)), label, current ? el('span', {class: 'stage-note'}, 'Current') : null));
    }
    return el('section', {class: 'surface progress-card'}, el('div', {class: 'card-label'}, 'Deployment progress'), el('div', {class: 'progress-top'}, el('strong', {}, stageNames[data.stage] || statusNames[data.status] || 'Preparing deployment'), el('span', {}, completed ? 'Complete' : failed ? 'Stopped' : '')), el('div', {class: 'progress-track', role: 'progressbar', 'aria-label': 'Completed deployment stages', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': percentage}, el('div', {class: `progress-fill progress-${percentage} ${failed ? 'failed' : ''}`})), list);
  }
  function hideCredentials() {
    clearInterval(state.secretTimer);
    state.secretTimer = null;
    state.secretDeadline = 0;
    state.secrets = null;
    state.secretRequest++;
    const current = $('#credentials-panel');
    if (current && state.detailId) current.replaceWith(renderCredentials());
  }
  function renderCredentials() {
    const revealed = state.secrets && state.secrets.deploymentId === state.detailId;
    const panel = el('section', {class: 'surface credentials-card', id: 'credentials-panel', 'aria-labelledby': 'credentials-title'});
    const header = el('div', {class: 'surface-header'}, el('h2', {class: 'credentials-title', id: 'credentials-title'}, icon('key'), 'Credentials'), revealed ? el('span', {class: 'credentials-timer', id: 'credentials-timer'}, `${Math.max(0, Math.ceil((state.secretDeadline - Date.now()) / 1000))}s remaining`) : null);
    const body = el('div', {class: 'credentials-body'});
    if (!revealed) {
      const reveal = button('Reveal access details', 'button-primary button-full', async () => {
        const deploymentId = state.detailId;
        const request = ++state.secretRequest;
        setBusy(reveal, 'Retrieving credentials…');
        const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true}); body.prepend(errorBox);
        try {
          const data = await api(`/api/deployments/${encodeURIComponent(deploymentId)}/credentials`, {method: 'POST'});
          if (request !== state.secretRequest || deploymentId !== state.detailId || !state.session) return;
          state.secrets = {...data, deploymentId};
          state.secretDeadline = Date.now() + 60000;
          panel.replaceWith(renderCredentials());
          clearInterval(state.secretTimer);
          state.secretTimer = setInterval(() => {
            const seconds = Math.ceil((state.secretDeadline - Date.now()) / 1000);
            if (seconds <= 0) { hideCredentials(); return; }
            const timer = $('#credentials-timer'); if (timer) timer.textContent = `${seconds}s remaining`;
          }, 1000);
        } catch (error) { inlineError(errorBox, error.message); reveal.disabled = false; reveal.replaceChildren(icon('eye'), 'Reveal access details'); }
      }, 'eye');
      body.append(el('p', {}, 'Find the Linux login, VM addresses, and service credentials for this deployment.'), reveal, el('p', {class: 'credentials-hint'}, 'Reveals are audited. Details hide automatically after 60 seconds or when you leave this page.'));
    } else {
      if (!state.secrets.vms?.length) body.append(el('p', {}, 'No credentials are available for this deployment yet.'));
      for (const vm of state.secrets.vms || []) {
        const section = el('div', {class: 'secret-vm'}, el('h4', {}, vm.name), el('p', {class: 'secret-role'}, `${roleName(vm.role)} · ${vm.ip || 'IP address pending'}`));
        section.append(secretField('Linux username', vm.username || ''), secretField('Linux password', vm.password || ''));
        for (const service of vm.services || []) {
          section.append(el('div', {class: 'secret-service'}, service.name || 'Service'));
          if (service.url) section.append(el('div', {class: 'service-links'}, serviceLink(service)));
          if (service.username) section.append(secretField('Service username', service.username));
          if (service.password) section.append(secretField('Service password', service.password));
        }
        if (vm.ssh_host_key) section.append(el('details', {class: 'host-key'}, el('summary', {}, 'SSH host key'), el('code', {}, vm.ssh_host_key)));
        body.append(section);
      }
      body.append(button('Hide credentials now', 'button-full section-spacer', hideCredentials, 'eye'), el('p', {class: 'credentials-hint'}, 'Copied values may remain on your clipboard after these details are hidden.'));
    }
    panel.append(header, body);
    return panel;
  }
  let secretFieldId = 0;
  function secretField(label, value) {
    const id = `secret-${++secretFieldId}`;
    const input = el('input', {id, value, readOnly: true, type: 'text', autocomplete: 'off', spellcheck: 'false', 'aria-label': label});
    const copy = el('button', {type: 'button', class: 'icon-button', title: `Copy ${label.toLowerCase()}`, 'aria-label': `Copy ${label.toLowerCase()}`, onClick: async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(value);
        copy.replaceChildren(icon('check'));
        copy.setAttribute('aria-label', 'Copied');
        setTimeout(() => { if (copy.isConnected) { copy.replaceChildren(icon('copy')); copy.setAttribute('aria-label', `Copy ${label.toLowerCase()}`); } }, 1800);
        notify(`${label} copied. Clear your clipboard when finished.`);
      } catch {
        input.focus(); input.select();
        notify('Copy is unavailable in this browser. The value is selected for manual copying.');
      }
    }}, icon('copy'));
    return el('div', {class: 'secret-field'}, el('label', {for: id}, label), el('div', {class: 'secret-input-row'}, input, copy));
  }
  async function refreshDetail(manual = false) {
    if (!state.session || setupRequired()) return;
    const id = state.detailId; const epoch = state.routeEpoch;
    if (!id) return;
    try {
      const data = await api(`/api/deployments/${encodeURIComponent(id)}`);
      if (id !== state.detailId || epoch !== state.routeEpoch) return;
      if (JSON.stringify(data) !== JSON.stringify(state.detail)) { state.detail = data; renderDetail(data); }
      if (manual) notify('Deployment details refreshed.');
    } catch (error) { if (manual || epoch === state.routeEpoch) globalError(error.message); }
  }
  function openRedeploy(data) {
    hideCredentials();
    const dialog = $('#confirm-dialog');
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const confirm = el('input', {type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: data.name, required: true, 'aria-describedby': 'confirm-description'});
    const cancel = button('Cancel', '', () => dialog.close());
    const submit = button(data.status === 'cleanup_failed' ? 'Retry cleanup & redeploy' : 'Delete & redeploy', 'button-danger', async () => {
      if (confirm.value !== data.name) return;
      inlineError(errorBox, ''); setBusy(submit, 'Cleaning up…'); cancel.disabled = true; confirm.disabled = true;
      dialog.dataset.busy = 'true';
      try {
        const replacement = await api(`/api/deployments/${encodeURIComponent(data.id)}/redeploy`, {method: 'POST', body: {confirm_name: confirm.value}});
        dialog.close();
        notify('Previous resources removed. A fresh deployment is queued.');
        goDetail(replacement.id);
      } catch (error) { inlineError(errorBox, error.message); refreshDetail(); }
      finally { delete dialog.dataset.busy; cancel.disabled = false; confirm.disabled = false; submit.disabled = confirm.value !== data.name; submit.replaceChildren(icon('refresh'), 'Delete & redeploy'); }
    }, 'refresh');
    submit.disabled = true;
    confirm.addEventListener('input', () => { submit.disabled = confirm.value !== data.name; });
    dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('alert'), el('h2', {id: 'confirm-title'}, 'Delete and start again?')), el('div', {class: 'confirm-body'}, el('p', {id: 'confirm-description'}, 'This permanently deletes the VMs, their disks, and installation media owned by ', el('strong', {}, data.name), '. A new deployment uses the same VM configuration, the current OS ISO selection in Setup, and freshly generated credentials. Shared datastores and unrelated VMs are preserved.'), el('p', {}, 'If cleanup fails, GDeploy stops before creating a replacement.'), errorBox, field(`Type “${data.name}” to confirm`, confirm), el('div', {class: 'confirm-actions'}, cancel, submit)));
    dialog.showModal();
    confirm.focus();
  }
  $('#confirm-dialog').addEventListener('cancel', event => { if ($('#confirm-dialog').dataset.busy) event.preventDefault(); });

  async function openWizard() {
    if (!state.session || setupRequired()) return;
    if (!state.settings?.configured || !state.settings?.iso_configured) { location.hash = state.settings?.configured ? 'settings/media' : 'settings/connection'; notify('Complete Setup with an ESXi connection and an OS ISO to begin a deployment.'); return; }
    hideCredentials();
    const dialog = $('#wizard-dialog');
    state.wizard = {step: 0, name: '', selected: new Set(['elasticsearch', 'kibana']), vms: {}, activeRole: 'elasticsearch', accepted: false, preflight: null, busy: true, error: ''};
    dialog.replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), loading('Loading ESXi datastores and networks…')));
    dialog.showModal();
    const wizard = state.wizard;
    try {
      state.inventory = await api('/api/inventory');
      if (state.wizard !== wizard) return;
      wizard.busy = false;
      renderWizard();
    } catch (error) {
      if (state.wizard !== wizard) return;
      wizard.busy = false;
      dialog.replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), el('div', {class: 'wizard-content'}, el('h3', {}, 'Couldn’t load your ESXi inventory'), el('p', {class: 'muted'}, 'Check the saved connection and your host’s certificate, permissions, and availability.'), el('div', {class: 'alert alert-error', role: 'alert'}, error.message), button('Open Setup', 'button-primary', () => { closeWizard(); location.hash = 'settings/connection'; }, 'server'))));
    }
  }
  function closeWizard() {
    if (state.wizard?.busy) return;
    $('#wizard-dialog').close(); state.wizard = null;
  }
  $('#wizard-dialog').addEventListener('cancel', event => { if (state.wizard?.busy) event.preventDefault(); else state.wizard = null; });
  function wizardHeader() {
    const close = el('button', {type: 'button', class: 'icon-button dialog-close', 'aria-label': 'Close deployment wizard', onClick: closeWizard}, icon('close'));
    return el('div', {class: 'wizard-header'}, el('div', {}, el('h2', {id: 'wizard-title'}, 'New deployment'), el('p', {}, 'A ready-to-use environment, configured your way.')), close);
  }
  function selectedRoles() { return Object.keys(roles).filter(role => state.wizard.selected.has(role)); }
  function defaultVMName(name, role) { return `${(name || 'environment').slice(0, 62 - role.length).replace(/-+$/, '')}-${role}`; }
  function ensureVMs() {
    const wizard = state.wizard;
    for (const role of selectedRoles()) {
      if (!wizard.vms[role]) wizard.vms[role] = {role, name: defaultVMName(wizard.name, role), cpu: roles[role].cpu, ram_gb: roles[role].ram, disk_gb: roles[role].disk, datastore: state.inventory?.datastores?.[0]?.name || '', network: state.inventory?.networks?.[0]?.name || '', ip_mode: 'dhcp', address: '', gateway: '', dnsText: ''};
    }
    if (!wizard.selected.has(wizard.activeRole)) wizard.activeRole = selectedRoles()[0];
  }
  function invalidatePreflight() { if (state.wizard) { state.wizard.preflight = null; state.wizard.error = ''; } }
  function renderWizard() {
    const wizard = state.wizard; if (!wizard) return;
    const focusedId = $('#wizard-dialog').contains(document.activeElement) ? document.activeElement.id : '';
    ensureVMs();
    const sidebar = el('aside', {class: 'wizard-sidebar', 'aria-label': 'Deployment steps'});
    ['Choose software', 'Configure VMs', 'Review & deploy'].forEach((label, index) => sidebar.append(el('div', {class: `wizard-step ${index === wizard.step ? 'active' : index < wizard.step ? 'done' : ''}`, ...(index === wizard.step ? {'aria-current': 'step'} : {})}, el('b', {}, index < wizard.step ? '✓' : index + 1), label)));
    sidebar.append(el('p', {class: 'wizard-aside-note'}, 'The OS ISO selected in Setup is used for each VM. Every selected role gets its own dedicated machine.'));
    const content = el('div', {class: 'wizard-content', id: 'wizard-content'});
    if (wizard.error) content.append(el('div', {class: 'alert alert-error', role: 'alert'}, wizard.error));
    if (wizard.step === 0) renderBlueprint(content);
    if (wizard.step === 1) renderResources(content);
    if (wizard.step === 2) renderReview(content);
    const footer = el('div', {class: 'wizard-footer'}, el('span', {}, `Step ${wizard.step + 1} of 3`));
    const actions = el('div', {class: 'wizard-footer-actions'});
    if (wizard.step > 0) actions.append(button('Back', 'button-ghost', () => { if (wizard.busy) return; wizard.step--; renderWizard(); }));
    else actions.append(button('Cancel', 'button-ghost', closeWizard));
    const nextLabel = wizard.step < 2 ? 'Continue' : wizard.preflight?.ok ? `Deploy ${selectedRoles().length} ${selectedRoles().length === 1 ? 'VM' : 'VMs'}` : 'Run preflight';
    const next = button(nextLabel, 'button-primary', wizardNext, wizard.step < 2 ? 'arrow' : wizard.preflight?.ok ? 'plus' : 'shield');
    if (wizard.busy) setBusy(next, wizard.preflight?.ok ? 'Queuing deployment…' : 'Checking prerequisites…');
    for (const action of actions.children) action.disabled = wizard.busy;
    actions.append(next); footer.append(actions);
    $('#wizard-dialog').replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), el('div', {class: 'wizard-layout'}, sidebar, content), footer));
    if (wizard.busy) for (const control of $('#wizard-dialog').querySelectorAll('button,input,select')) control.disabled = true;
    else if (focusedId) document.getElementById(focusedId)?.focus({preventScroll: true});
  }
  function renderBlueprint(content) {
    const wizard = state.wizard;
    content.append(el('h3', {}, 'What are we deploying?'), el('p', {class: 'muted'}, 'Name your environment, then choose the software to provision. You can adjust every VM in the next step.'));
    const name = el('input', {id: 'deployment-name', type: 'text', value: wizard.name, required: true, maxlength: 63, pattern: '[a-z](?:(?:[a-z0-9]|-)*[a-z0-9])?', placeholder: 'e.g. observability-lab', autocomplete: 'off', spellcheck: 'false', onInput: event => {
      const previous = wizard.name;
      wizard.name = event.target.value; invalidatePreflight();
      for (const role of Object.keys(wizard.vms)) if (wizard.vms[role].name === defaultVMName(previous, role)) wizard.vms[role].name = defaultVMName(wizard.name, role);
    }});
    content.append(field('Deployment name', name, 'Use lowercase letters, numbers, and hyphens. VM names use this as a starting point.'));
    const grid = el('div', {class: 'role-grid'});
    for (const [role, config] of Object.entries(roles)) {
      const checkbox = el('input', {id: `role-${role}`, type: 'checkbox', checked: wizard.selected.has(role), 'aria-label': config.name, onChange: event => {
        if (event.target.checked) wizard.selected.add(role); else wizard.selected.delete(role);
        if (role === 'kibana' && event.target.checked) wizard.selected.add('elasticsearch');
        if (role === 'elasticsearch' && !event.target.checked) wizard.selected.delete('kibana');
        invalidatePreflight(); renderWizard();
      }});
      grid.append(el('label', {class: 'role-option'}, checkbox, roleIcon(role), el('span', {class: 'role-option-text'}, el('strong', {}, config.name), el('small', {}, config.description))));
    }
    content.append(el('div', {class: 'field-section-label'}, 'Software & roles', el('span', {}, `${wizard.selected.size} ${wizard.selected.size === 1 ? 'VM' : 'VMs'} selected`)), grid, el('div', {class: 'wizard-callout'}, icon('info'), el('span', {}, 'Selecting Kibana also selects Elasticsearch. GDeploy configures their connection, matching versions, and TLS. Splunk runs independently on its own VM.')));
  }
  function renderResources(content) {
    const wizard = state.wizard;
    content.append(el('h3', {}, 'Make room for your workloads'), el('p', {class: 'muted'}, 'Configure resources, storage placement, and networking for each VM. Defaults are a starting point for a lab environment.'));
    const tabs = el('div', {class: 'vm-tabs', role: 'tablist', 'aria-label': 'Virtual machines'});
    for (const role of selectedRoles()) tabs.append(el('button', {type: 'button', role: 'tab', class: `vm-tab ${wizard.activeRole === role ? 'active' : ''}`, 'aria-selected': wizard.activeRole === role ? 'true' : 'false', 'aria-controls': 'vm-resource-form', id: `tab-${role}`, tabindex: wizard.activeRole === role ? '0' : '-1', onKeydown: event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const list = selectedRoles(); const index = list.indexOf(role); wizard.activeRole = event.key === 'Home' ? list[0] : event.key === 'End' ? list[list.length - 1] : list[(index + (event.key === 'ArrowRight' ? 1 : -1) + list.length) % list.length]; renderWizard(); document.getElementById(`tab-${wizard.activeRole}`)?.focus(); }, onClick: () => { wizard.activeRole = role; renderWizard(); }}, icon(roles[role].icon), roles[role].short));
    content.append(tabs);
    const vm = wizard.vms[wizard.activeRole]; if (!vm) return;
    const form = el('div', {class: 'vm-resource-form', id: 'vm-resource-form', role: 'tabpanel', 'aria-labelledby': `tab-${vm.role}`});
    const input = (key, attrs = {}) => el('input', {...attrs, value: vm[key], onInput: event => { vm[key] = ['cpu', 'ram_gb', 'disk_gb'].includes(key) ? Number(event.target.value) : event.target.value; invalidatePreflight(); }});
    form.append(el('div', {class: 'vm-form-header'}, roleIcon(vm.role), el('div', {}, el('h4', {}, roleName(vm.role)), el('p', {}, 'Operating system from your selected OS ISO'))), field('Virtual machine name', input('name', {type: 'text', required: true, maxlength: 63, pattern: '[a-z](?:(?:[a-z0-9]|-)*[a-z0-9])?', autocomplete: 'off', spellcheck: 'false'}), 'A unique hostname starting with a letter; lowercase letters, numbers, and hyphens only.'), el('div', {class: 'field-grid three'}, field('CPU cores', input('cpu', {type: 'number', min: 1, max: 128, step: 1, required: true}), 'vCPU'), field('Memory', input('ram_gb', {type: 'number', min: ({ubuntu: 2, elasticsearch: 8, kibana: 4, splunk: 4})[vm.role], max: 2048, step: 1, required: true}), 'GB RAM'), field('Disk size', input('disk_gb', {type: 'number', min: 25, max: 65536, step: 1, required: true}), 'GB · thin provisioned')));
    const datastore = el('select', {required: true, onChange: event => { vm.datastore = event.target.value; invalidatePreflight(); }}, el('option', {value: ''}, 'Choose a datastore'), (state.inventory?.datastores || []).map(store => el('option', {value: store.name}, `${store.name} · ${Math.floor(Number(store.free_gb))} GB free`))); datastore.value = vm.datastore;
    const network = el('select', {required: true, onChange: event => { vm.network = event.target.value; invalidatePreflight(); }}, el('option', {value: ''}, 'Choose a port group'), (state.inventory?.networks || []).map(net => el('option', {value: net.name}, net.name))); network.value = vm.network;
    form.append(el('div', {class: 'field-grid'}, field('Datastore', datastore, 'Storage destination on the ESXi host.'), field('Network / port group', network, 'Must be reachable from GDeploy.')), el('hr', {class: 'form-divider'}), el('div', {class: 'field-section-label'}, 'IP address configuration'));
    const choices = el('div', {class: 'network-choices'});
    for (const [mode, label] of [['dhcp', 'Automatic (DHCP)'], ['static', 'Static IP']]) choices.append(el('label', {}, el('input', {id: `network-${mode}`, type: 'radio', name: 'ip-mode', value: mode, checked: vm.ip_mode === mode, onChange: () => { vm.ip_mode = mode; invalidatePreflight(); renderWizard(); }}), label));
    form.append(choices);
    if (vm.ip_mode === 'static') form.append(el('div', {class: 'static-fields'}, el('div', {class: 'field-grid'}, field('IPv4 address / prefix', input('address', {type: 'text', required: true, placeholder: '192.168.1.20/24', autocomplete: 'off', spellcheck: 'false'}), 'Include the subnet prefix, for example /24.'), field('Default gateway', input('gateway', {type: 'text', required: true, placeholder: '192.168.1.1', autocomplete: 'off', spellcheck: 'false'}))), field('DNS servers', input('dnsText', {type: 'text', required: true, placeholder: '192.168.1.1, 1.1.1.1', autocomplete: 'off', spellcheck: 'false'}), 'Separate multiple IPv4 addresses with commas.')));
    else form.append(el('p', {class: 'network-note'}, 'A DHCP server on this network must provide an address, gateway, and DNS. GDeploy discovers the guest address through VMware Tools.'));
    content.append(form);
    if (selectedRoles().length > 1) content.append(el('div', {class: 'wizard-callout'}, icon('server'), el('span', {}, 'Use the tabs above to review every VM. Each machine can use its own datastore, network, and IP configuration.')));
  }
  function getSpec() {
    const wizard = state.wizard;
    return {name: wizard.name.trim(), splunk_license_accepted: wizard.accepted, vms: selectedRoles().map(role => {
      const vm = wizard.vms[role];
      return {role, name: vm.name.trim(), cpu: Number(vm.cpu), ram_gb: Number(vm.ram_gb), disk_gb: Number(vm.disk_gb), datastore: vm.datastore, network: vm.network, ip_mode: vm.ip_mode, address: vm.ip_mode === 'static' ? vm.address.trim() : null, gateway: vm.ip_mode === 'static' ? vm.gateway.trim() : null, dns: vm.ip_mode === 'static' ? vm.dnsText.split(',').map(value => value.trim()).filter(Boolean) : []};
    })};
  }
  function renderReview(content) {
    const wizard = state.wizard; const spec = getSpec();
    content.append(el('h3', {}, 'Ready for a final check'), el('p', {class: 'muted'}, 'Review ', el('strong', {}, spec.name), ', then run preflight. GDeploy checks your host, installation media, capacity, and configuration before provisioning.'));
    const summary = (value, label) => el('div', {}, el('strong', {}, value), el('span', {}, label));
    content.append(el('div', {class: 'wizard-summary'}, summary(spec.vms.length, 'DEDICATED VIRTUAL MACHINES'), summary(`${spec.vms.reduce((n, vm) => n + vm.cpu, 0)} vCPU`, `${spec.vms.reduce((n, vm) => n + vm.ram_gb, 0)} GB TOTAL MEMORY`), summary(`${spec.vms.reduce((n, vm) => n + vm.disk_gb, 0)} GB`, 'TOTAL VIRTUAL DISK CAPACITY')));
    const review = el('div', {});
    for (const vm of spec.vms) review.append(el('div', {class: 'review-vm'}, el('div', {class: 'review-vm-head'}, roleIcon(vm.role), el('strong', {}, vm.name)), el('small', {}, `${roleName(vm.role)} · ${vm.cpu} vCPU · ${vm.ram_gb} GB RAM · ${vm.disk_gb} GB disk`, el('br'), `${vm.datastore} · ${vm.network} · ${vm.ip_mode === 'dhcp' ? 'DHCP' : vm.address}`, vm.ip_mode === 'static' ? [el('br'), `Gateway ${vm.gateway} · DNS ${vm.dns.join(', ')}`] : null)));
    content.append(review);
    if (wizard.selected.has('splunk')) content.append(el('div', {class: 'license-box'}, el('label', {class: 'check-label'}, el('input', {type: 'checkbox', checked: wizard.accepted, onChange: event => { wizard.accepted = event.target.checked; invalidatePreflight(); renderWizard(); }}), el('span', {}, 'I have reviewed and accept the Splunk license terms applicable to the supplied package, and I authorize unattended acceptance during installation.'))));
    content.append(el('div', {class: 'wizard-callout'}, icon('key'), el('span', {}, 'Linux and application credentials are generated during provisioning. Reveal them from the Credentials panel on the deployment page.')));
    const preflight = el('div', {class: 'preflight-box'}, el('div', {class: 'preflight-title'}, el('h4', {}, 'Preflight checks'), el('span', {}, wizard.preflight ? wizard.preflight.ok ? 'All checks passed' : 'Resolve failed checks' : 'Not run yet')));
    if (wizard.preflight) {
      const checks = el('div', {}, (wizard.preflight.checks || []).map(check => el('div', {class: `preflight-check ${check.ok ? '' : 'fail'}`}, icon(check.ok ? 'checkCircle' : 'alert'), el('div', {}, el('strong', {}, check.name), el('p', {}, check.message)))));
      preflight.append(checks);
      if (wizard.preflight.ok) preflight.append(el('p', {class: 'credentials-hint'}, 'Deployment checks run again when you submit, then the job is added to the worker queue.'));
    } else preflight.append(el('p', {class: 'muted small'}, 'Run preflight to verify that this environment is ready to deploy.'));
    content.append(preflight);
  }
  const validHostname = value => /^[a-z](?:[a-z0-9-]*[a-z0-9])?$/.test(value) && value.length <= 63;
  const validIPv4 = value => /^\d{1,3}(?:\.\d{1,3}){3}$/.test(value) && value.split('.').every(part => Number(part) <= 255);
  function validateVMs() {
    const spec = getSpec(); const names = new Set();
    for (const vm of spec.vms) {
      let error = '';
      if (!validHostname(vm.name)) error = 'Use a VM name of 1–63 lowercase letters, numbers, or hyphens, beginning with a letter and ending with a letter or number.';
      else if (names.has(vm.name)) error = 'Every virtual machine needs a unique name.';
      else if (!Number.isInteger(vm.cpu) || vm.cpu < 1 || vm.cpu > 128) error = 'CPU cores must be a whole number from 1 to 128.';
      else if (!Number.isInteger(vm.ram_gb) || vm.ram_gb < ({ubuntu: 2, elasticsearch: 8, kibana: 4, splunk: 4})[vm.role] || vm.ram_gb > 2048) error = `Memory must be a whole number from ${({ubuntu: 2, elasticsearch: 8, kibana: 4, splunk: 4})[vm.role]} to 2048 GB for this role.`;
      else if (!Number.isInteger(vm.disk_gb) || vm.disk_gb < 25 || vm.disk_gb > 65536) error = 'Disk size must be a whole number from 25 to 65536 GB.';
      else if (!vm.datastore || !vm.network) error = 'Choose a datastore and network for this VM.';
      else if (vm.ip_mode === 'static') {
        const [address, prefix, extra] = vm.address.split('/');
        if (!validIPv4(address) || extra !== undefined || !/^\d{1,2}$/.test(prefix || '') || Number(prefix) < 1 || Number(prefix) > 30) error = 'Enter an IPv4 address with a subnet prefix from /1 to /30, for example 192.168.1.20/24.';
        else if (!validIPv4(vm.gateway)) error = 'Enter a valid IPv4 default gateway.';
        else if (!vm.dns.length || vm.dns.length > 4 || vm.dns.some(dns => !validIPv4(dns))) error = 'Enter one to four valid IPv4 DNS servers, separated by commas.';
      }
      names.add(vm.name);
      if (error) return {role: vm.role, error: `${roleName(vm.role)}: ${error}`};
    }
    return null;
  }
  async function wizardNext() {
    const wizard = state.wizard; if (!wizard || wizard.busy) return;
    wizard.error = '';
    if (wizard.step === 0) {
      wizard.name = wizard.name.trim();
      if (!validHostname(wizard.name)) wizard.error = 'Choose a deployment name of 1–63 lowercase letters, numbers, or hyphens, beginning with a letter and ending with a letter or number.';
      else if (!wizard.selected.size) wizard.error = 'Select at least one software role or OS only.';
      else { ensureVMs(); wizard.step = 1; }
      renderWizard(); return;
    }
    if (wizard.step === 1) {
      const invalid = validateVMs();
      if (invalid) { wizard.error = invalid.error; wizard.activeRole = invalid.role; }
      else wizard.step = 2;
      renderWizard(); return;
    }
    if (wizard.selected.has('splunk') && !wizard.accepted) { wizard.error = 'Accept the license terms for the supplied Splunk package before running preflight.'; renderWizard(); return; }
    const deploy = Boolean(wizard.preflight?.ok);
    const spec = getSpec();
    wizard.busy = true; renderWizard();
    try {
      if (deploy) {
        const result = await api('/api/deployments', {method: 'POST', body: spec});
        wizard.busy = false; closeWizard();
        notify('Deployment queued. Follow its progress here.');
        goDetail(result.id);
        return;
      }
      const result = await api('/api/preflight', {method: 'POST', body: spec});
      if (state.wizard !== wizard) return;
      wizard.preflight = result;
      if (!result.ok) wizard.error = 'Some checks need attention. Review the results below, update the configuration, and run preflight again.';
    } catch (error) { wizard.error = error.message; if (deploy) wizard.preflight = null; }
    finally { if (state.wizard === wizard) { wizard.busy = false; renderWizard(); $('#wizard-content')?.scrollTo({top: 0}); } }
  }
  async function poll() {
    if (!state.session || setupRequired() || document.hidden || state.pollBusy) return;
    state.pollBusy = true;
    const epoch = state.routeEpoch;
    try {
      if (state.route === 'detail') await refreshDetail();
      else if (state.route === 'deployments') {
        const rows = await api('/api/deployments');
        if (epoch === state.routeEpoch && JSON.stringify(rows) !== JSON.stringify(state.deployments)) {
          const activeElement = document.activeElement;
          const editing = activeElement?.classList.contains('search-field');
          const selection = editing ? activeElement.selectionStart : null;
          state.deployments = rows; renderOverview();
          if (editing) { const input = $('.search-field'); input?.focus(); if (selection !== null) input?.setSelectionRange(selection, selection); }
        }
      }
    } catch (error) { if (epoch === state.routeEpoch) globalError(error.message); }
    finally { state.pollBusy = false; }
  }
  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const submit = $('#login-submit');
    inlineError($('#login-error'), ''); inlineError($('#login-success'), ''); setBusy(submit, 'Signing in…');
    try {
      const session = await api('/api/login', {method: 'POST', body: {username: $('#login-username').value.trim(), password: $('#login-password').value}});
      $('#login-password').value = '';
      await showApp(session);
    } catch (error) { inlineError($('#login-error'), error.message); }
    finally { submit.disabled = false; submit.replaceChildren('Sign in ', icon('arrow')); }
  });
  for (const input of $('#setup-form').querySelectorAll('input')) input.addEventListener('input', () => {
    input.setCustomValidity('');
    $('#setup-confirm').setCustomValidity('');
  });
  $('#setup-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!setupRequired() || state.setupBusy) return;
    const usernameInput = $('#setup-username');
    const passwordInput = $('#setup-password');
    const confirmInput = $('#setup-confirm');
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    const passwordConfirm = confirmInput.value;
    for (const input of [usernameInput, passwordInput, confirmInput]) input.setCustomValidity('');
    let invalidInput;
    let message;
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$/.test(username) || username.toLowerCase() === 'admin') {
      invalidInput = usernameInput;
      message = 'Choose a username other than admin: 3–100 letters, numbers, dots, underscores or hyphens, starting with a letter or number.';
    } else if ([...password].length < 12 || [...password].length > 1024 || !password.trim()) {
      invalidInput = passwordInput;
      message = 'Choose a password of 12–1,024 characters that is not only spaces.';
    } else if (password !== passwordConfirm) {
      invalidInput = confirmInput;
      message = 'The new passwords do not match.';
    }
    if (invalidInput) {
      invalidInput.setCustomValidity(message);
      inlineError($('#setup-error'), message);
      invalidInput.reportValidity();
      invalidInput.focus();
      return;
    }
    inlineError($('#setup-error'), '');
    const submit = $('#setup-submit');
    state.setupBusy = true;
    $('#setup-logout').disabled = true;
    setBusy(submit, 'Saving your login…');
    try {
      await api('/api/account/setup', {method: 'POST', body: {username, password, password_confirm: passwordConfirm}});
      showLogin('', {username, success: 'Administrator login updated. Sign in with your new credentials.'});
      $('#login-password').focus();
    } catch (error) {
      if (setupRequired()) inlineError($('#setup-error'), error.message);
    } finally {
      state.setupBusy = false;
      $('#setup-logout').disabled = false;
      submit.disabled = false;
      submit.replaceChildren('Save administrator login ', icon('arrow'));
    }
  });
  $('#setup-logout').addEventListener('click', logout);
  $('#logout').addEventListener('click', logout);
  const mobileLogout = el('button', {type: 'button', class: 'icon-button mobile-logout', 'aria-label': 'Sign out', onClick: logout}, icon('logout'));
  $('.topbar').append(mobileLogout);
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (document.hidden) hideCredentials(); else poll(); });
  window.addEventListener('pagehide', hideCredentials);
  setInterval(poll, 5000);
  api('/api/session').then(session => session?.authenticated ? showApp(session) : showLogin()).catch(error => showLogin(error.message));
})();
