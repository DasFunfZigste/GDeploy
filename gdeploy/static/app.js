'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const page = $('#page');
  const state = {
    session: null, settings: null, inventory: null, deployments: [], detail: null,
    route: 'deployments', detailId: null, routeEpoch: 0, pollBusy: false,
    search: '', filter: 'all', wizard: null, secrets: null, secretTimer: null,
    secretDeadline: 0, secretRequest: 0, toastTimer: null,
  };
  const roles = {
    ubuntu: {name: 'Ubuntu server', short: 'Ubuntu', description: 'A clean Linux server, ready for your own workloads.', cpu: 2, ram: 4, disk: 40, icon: 'terminal'},
    elasticsearch: {name: 'Elasticsearch', short: 'Elasticsearch', description: 'Search and analytics, with TLS enabled.', cpu: 2, ram: 8, disk: 60, icon: 'layers'},
    kibana: {name: 'Kibana', short: 'Kibana', description: 'Visualize your data. Connected to Elasticsearch.', cpu: 2, ram: 4, disk: 40, icon: 'chart'},
    splunk: {name: 'Splunk Enterprise', short: 'Splunk', description: 'Search, monitor, and analyze machine data.', cpu: 4, ram: 8, disk: 60, icon: 'activity'},
  };
  const stageOrder = ['queued', 'preflight', 'preparing', 'creating', 'installing_os', 'installing_software', 'verifying', 'completed'];
  const stageNames = {queued: 'Waiting in queue', preflight: 'Checking prerequisites', preparing: 'Preparing installation media', creating: 'Creating virtual machines', installing_os: 'Installing Ubuntu', installing_software: 'Installing software', verifying: 'Verifying services', completed: 'Ready to use', failed: 'Deployment failed', interrupted: 'Deployment interrupted', cleaning: 'Removing deployment resources', cleanup_failed: 'Cleanup needs attention', reverted: 'Resources removed'};
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
    if (data && Array.isArray(data.detail)) return errorText(data.detail);
    return fallback;
  }
  async function api(path, options = {}) {
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
  function showLogin(message = '') {
    state.session = null;
    state.routeEpoch++;
    hideCredentials();
    state.wizard = null;
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
    $('#boot').hidden = true;
    $('#app').hidden = true;
    $('#login-screen').hidden = false;
    $('#login-password').value = '';
    inlineError($('#login-error'), message);
  }
  async function showApp(session) {
    state.session = session;
    $('#boot').hidden = true;
    $('#login-screen').hidden = true;
    $('#app').hidden = false;
    $('#account-name').textContent = session.username || 'Administrator';
    $('#account-avatar').textContent = (session.username || 'A').slice(0, 1);
    await route();
  }
  async function logout() {
    try { await api('/api/logout', {method: 'POST'}); showLogin(); }
    catch (error) { globalError(error.message); }
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
    const epoch = ++state.routeEpoch;
    globalError('');
    hideCredentials();
    state.detail = null;
    state.detailId = null;
    page.replaceChildren(loading('Loading your workspace…'));
    const hash = location.hash.slice(1);
    if (hash === 'settings') {
      state.route = 'settings';
      markNav('settings');
      $('#breadcrumb').textContent = 'ESXi connection';
      try {
        const settings = await api('/api/settings');
        if (epoch !== state.routeEpoch) return;
        state.settings = settings;
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
    const setupBanner = !state.settings?.configured ? el('div', {class: 'connection-banner'}, icon('server'), el('div', {}, el('strong', {}, 'Connect your ESXi host'), el('p', {}, 'Set up your host connection before creating your first deployment.')), el('a', {href: '#settings', class: 'button button-small'}, 'Configure connection', icon('arrow'))) : null;
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
      body.append(el('tr', {}, el('td', {}, el('a', {class: 'deployment-name', href: `#deployment/${encodeURIComponent(row.id)}`}, row.name), el('div', {class: 'subline'}, vmRows.map(vm => (roles[vm.role] || {short: vm.role}).short).join(' · ') || 'Ubuntu 24.04 LTS')), el('td', {}, statusBadge(row.status)), el('td', {}, `${vmRows.length} ${vmRows.length === 1 ? 'VM' : 'VMs'}`, el('div', {class: 'subline'}, `${vmRows.reduce((n, vm) => n + Number(vm.cpu || 0), 0)} vCPU · ${vmRows.reduce((n, vm) => n + Number(vm.ram_gb || 0), 0)} GB RAM`)), el('td', {}, el('time', {datetime: row.created_at || ''}, date(row.created_at, true))), el('td', {}, el('a', {class: 'table-arrow', href: `#deployment/${encodeURIComponent(row.id)}`, 'aria-label': `View ${row.name}`}, icon('arrow')))));
    }
    container.replaceChildren(el('div', {class: 'table-scroll'}, el('table', {}, el('thead', {}, el('tr', {}, ...['Deployment', 'Status', 'Resources', 'Created', ''].map(text => el('th', {scope: 'col'}, text)))), body)));
  }
  function renderSettings() {
    const settings = state.settings || {};
    const host = el('input', {id: 'esxi-host', name: 'host', required: true, value: settings.host || '', placeholder: 'esxi.example.com', autocomplete: 'off', spellcheck: 'false'});
    const username = el('input', {id: 'esxi-username', name: 'username', required: true, value: settings.username || '', placeholder: 'Your ESXi service account', autocomplete: 'off', spellcheck: 'false'});
    const password = el('input', {id: 'esxi-password', name: 'password', type: 'password', required: !settings.configured, placeholder: settings.configured ? 'Leave blank to keep the saved password' : 'ESXi account password', autocomplete: 'new-password'});
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const resultBox = el('div', {hidden: true});
    const save = button('Save connection', 'button-primary', null, 'check'); save.type = 'submit';
    const test = button('Test saved connection', '', async () => {
      inlineError(errorBox, ''); resultBox.hidden = true; setBusy(test, 'Connecting…');
      try {
        const inventory = await api('/api/inventory'); state.inventory = inventory;
        resultBox.className = 'alert alert-success';
        resultBox.replaceChildren(el('strong', {}, `Connected to ${inventory.host?.name || settings.host}`), el('div', {class: 'inventory-summary'}, el('span', {}, `${inventory.host?.cpu_threads || 0} CPU threads`), el('span', {}, `${inventory.host?.memory_gb || 0} GB memory`), el('span', {}, `${inventory.datastores?.length || 0} datastores`), el('span', {}, `${inventory.networks?.length || 0} networks`)));
        resultBox.hidden = false;
      } catch (error) { inlineError(errorBox, error.message); }
      finally { test.disabled = !state.settings?.configured; test.replaceChildren(icon('refresh'), 'Test saved connection'); }
    }, 'refresh');
    test.disabled = !settings.configured;
    const form = el('form', {class: 'surface', onSubmit: async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      inlineError(errorBox, ''); resultBox.hidden = true; setBusy(save, 'Saving…'); test.disabled = true;
      try {
        await api('/api/settings', {method: 'PUT', body: {host: host.value.trim(), username: username.value.trim(), password: password.value, verify_tls: true}});
        password.value = '';
        state.settings = await api('/api/settings'); state.inventory = null;
        if (state.route === 'settings') renderSettings();
        notify('ESXi connection saved. Test the connection to check access.');
      } catch (error) { inlineError(errorBox, error.message); }
      finally { save.disabled = false; save.replaceChildren(icon('check'), 'Save connection'); test.disabled = !state.settings?.configured; }
    }}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {}, 'Host credentials'), el('p', {}, 'GDeploy uses this account to provision and manage its VMs.')), settings.configured ? el('span', {class: 'status status-completed'}, 'Configured') : el('span', {class: 'status'}, 'Not configured')), el('div', {class: 'form-body'}, errorBox, resultBox, field('ESXi host', host, 'Use a hostname or IP address for your standalone ESXi host.'), field('Username', username, 'Use an account with the required VM, network, and datastore permissions.'), field('Password', password, settings.configured ? 'Changing the host or username requires entering its password again.' : 'Your saved password is never returned by the settings API.'), el('div', {class: 'secure-note'}, icon('shield'), el('div', {}, el('strong', {}, 'Certificate verification enabled'), 'The ESXi certificate must be trusted by GDeploy. Configure your certificate authority on the server if needed.'))), el('div', {class: 'form-footer'}, test, save));
    const item = (ready, title, description, symbol) => el('div', {class: `setup-item ${ready ? 'ready' : ''}`}, icon(ready ? 'checkCircle' : symbol), el('div', {}, el('strong', {}, title), el('p', {}, description)));
    const readiness = el('section', {class: 'surface'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Deployment prerequisites')), el('div', {class: 'setup-list'}, item(settings.configured, 'ESXi host', settings.configured ? 'Connection details saved. Use the connection test to verify access.' : 'Save your host credentials to load inventory.', 'server'), item(settings.iso_configured, 'Ubuntu 24.04 installation media', settings.iso_configured ? 'Installation media configured. Preflight checks the file before deployment.' : 'Configure a local Ubuntu 24.04 live-server ISO on the GDeploy server.', 'disc'), item(settings.splunk_configured, 'Splunk installation package', settings.splunk_configured ? 'Splunk package configured. Its license must be accepted for each deployment.' : 'Optional: configure a Splunk Enterprise Linux x86_64 .tgz package on the server.', 'layers')));
    page.replaceChildren(heading('ESXi connection', 'Connect your infrastructure and prepare for deployment.'), el('div', {class: 'settings-grid'}, form, el('aside', {}, readiness, el('p', {class: 'settings-note'}, 'Installation media and package paths are configured on the GDeploy server. Elasticsearch and Kibana are installed from Elastic’s package repository; guests need outbound network access.'))));
  }
  function field(label, input, hint) { return el('label', {class: 'field'}, el('span', {}, label), input, hint ? el('small', {}, hint) : null); }
  function renderDetail(data) {
    const vms = data.vms || [];
    const back = el('a', {class: 'back-link', href: '#deployments'}, icon('back'), 'All deployments');
    const title = el('div', {class: 'page-heading'}, el('div', {}, el('div', {class: 'detail-title'}, el('h1', {}, data.name), statusBadge(data.status)), el('p', {class: 'detail-meta'}, `${vms.length} ${vms.length === 1 ? 'virtual machine' : 'virtual machines'} · Ubuntu 24.04 LTS · Created ${date(data.created_at, true)}`)), button('Refresh', 'button-ghost button-small', () => refreshDetail(true), 'refresh'));
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
    if (failureStatuses.has(data.status)) aside.append(el('section', {class: 'surface recovery-card'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Recover this deployment')), el('div', {class: 'recovery-body'}, el('p', {}, 'Delete the VMs and installation media owned by this failed deployment, then start again with the same configuration. All data on those VM disks will be deleted.'), button(data.status === 'cleanup_failed' ? 'Retry cleanup & redeploy' : 'Delete & redeploy', 'button-danger button-full', () => openRedeploy(data), 'refresh'))));
    page.replaceChildren(back, title, el('div', {class: 'detail-layout'}, left, aside));
  }
  function renderProgress(data) {
    const index = stageOrder.indexOf(data.stage);
    const completed = data.status === 'completed';
    const failed = failureStatuses.has(data.status);
    const percentage = completed ? 100 : index > 0 ? Math.round(index / (stageOrder.length - 1) * 100) : 0;
    const list = el('ol', {class: 'stage-list'});
    const stages = [['preflight', 'Preflight checks'], ['preparing', 'Prepare installation media'], ['creating', 'Create virtual machines'], ['installing_os', 'Install Ubuntu 24.04'], ['installing_software', 'Install selected software'], ['verifying', 'Verify services']];
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
    dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('alert'), el('h2', {id: 'confirm-title'}, 'Delete and start again?')), el('div', {class: 'confirm-body'}, el('p', {id: 'confirm-description'}, 'This permanently deletes the VMs, their disks, and installation media owned by ', el('strong', {}, data.name), '. A new deployment uses the same configuration and freshly generated credentials. Shared datastores and unrelated VMs are preserved.'), el('p', {}, 'If cleanup fails, GDeploy stops before creating a replacement.'), errorBox, field(`Type “${data.name}” to confirm`, confirm), el('div', {class: 'confirm-actions'}, cancel, submit)));
    dialog.showModal();
    confirm.focus();
  }
  $('#confirm-dialog').addEventListener('cancel', event => { if ($('#confirm-dialog').dataset.busy) event.preventDefault(); });

  async function openWizard() {
    if (!state.settings?.configured) { location.hash = 'settings'; notify('Save your ESXi connection to begin a deployment.'); return; }
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
      dialog.replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), el('div', {class: 'wizard-content'}, el('h3', {}, 'Couldn’t load your ESXi inventory'), el('p', {class: 'muted'}, 'Check the saved connection and your host’s certificate, permissions, and availability.'), el('div', {class: 'alert alert-error', role: 'alert'}, error.message), button('Open ESXi connection', 'button-primary', () => { closeWizard(); location.hash = 'settings'; }, 'server'))));
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
    sidebar.append(el('p', {class: 'wizard-aside-note'}, 'Ubuntu 24.04 LTS is installed on each VM. Every selected role gets its own dedicated machine.'));
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
    form.append(el('div', {class: 'vm-form-header'}, roleIcon(vm.role), el('div', {}, el('h4', {}, roleName(vm.role)), el('p', {}, 'Ubuntu 24.04 LTS · Linux x86_64'))), field('Virtual machine name', input('name', {type: 'text', required: true, maxlength: 63, pattern: '[a-z](?:(?:[a-z0-9]|-)*[a-z0-9])?', autocomplete: 'off', spellcheck: 'false'}), 'A unique hostname starting with a letter; lowercase letters, numbers, and hyphens only.'), el('div', {class: 'field-grid three'}, field('CPU cores', input('cpu', {type: 'number', min: 1, max: 128, step: 1, required: true}), 'vCPU'), field('Memory', input('ram_gb', {type: 'number', min: ({ubuntu: 2, elasticsearch: 8, kibana: 4, splunk: 4})[vm.role], max: 2048, step: 1, required: true}), 'GB RAM'), field('Disk size', input('disk_gb', {type: 'number', min: 25, max: 65536, step: 1, required: true}), 'GB · thin provisioned')));
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
      else if (!wizard.selected.size) wizard.error = 'Select at least one software role or Ubuntu server.';
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
    if (!state.session || document.hidden || state.pollBusy) return;
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
    inlineError($('#login-error'), ''); setBusy(submit, 'Signing in…');
    try {
      const session = await api('/api/login', {method: 'POST', body: {username: $('#login-username').value.trim(), password: $('#login-password').value}});
      $('#login-password').value = '';
      await showApp(session);
    } catch (error) { inlineError($('#login-error'), error.message); }
    finally { submit.disabled = false; submit.replaceChildren('Sign in ', icon('arrow')); }
  });
  $('#logout').addEventListener('click', logout);
  const mobileLogout = el('button', {type: 'button', class: 'icon-button mobile-logout', 'aria-label': 'Sign out', onClick: logout}, icon('logout'));
  $('.topbar').append(mobileLogout);
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (document.hidden) hideCredentials(); else poll(); });
  window.addEventListener('pagehide', hideCredentials);
  setInterval(poll, 5000);
  api('/api/session').then(session => session?.authenticated ? showApp(session) : showLogin()).catch(error => showLogin(error.message));
})();
