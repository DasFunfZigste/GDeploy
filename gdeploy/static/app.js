'use strict';

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const page = $('#page');
  const state = {
    session: null, settings: null, inventory: null, inventoryHost: null, deployments: [], detail: null, detailPending: null,
    route: 'deployments', detailId: null, detailRequest: 0, routeEpoch: 0, pollBusy: false,
    search: '', filter: 'all', includeHidden: false, historyRevision: 0, visibilityBusy: new Set(), stopBusy: new Set(), wizard: null, secrets: null, secretTimer: null,
    secretDeadline: 0, secretRequest: 0, toastTimer: null,
    setupBusy: false, mediaUpload: null, setupTab: null, packageTab: 'splunk', openLogs: new Set(),
  };
  const roles = {
    ubuntu: {name: 'OS only', short: 'OS only', description: 'Install the operating system without additional software.', cpu: 2, ram: 4, disk: 40, icon: 'terminal'},
    elasticsearch: {name: 'Elasticsearch', short: 'Elasticsearch', description: 'Search and analytics, with TLS enabled.', cpu: 2, ram: 8, disk: 60, icon: 'layers'},
    kibana: {name: 'Kibana', short: 'Kibana', description: 'Visualize your data. Connected to Elasticsearch.', cpu: 2, ram: 4, disk: 40, icon: 'chart'},
    splunk: {name: 'Splunk Enterprise', short: 'Splunk', description: 'Search, monitor, and analyze machine data.', cpu: 4, ram: 8, disk: 60, icon: 'activity'},
    fleetmanager: {name: 'FleetManager', short: 'FleetManager', description: 'Manage your Corelight sensors from a dedicated server.', cpu: 2, ram: 8, disk: 80, minCpu: 2, minRam: 8, minDisk: 60, icon: 'server'},
  };
  const stageOrder = ['queued', 'preflight', 'preparing', 'creating', 'installing_os', 'installing_software', 'verifying', 'completed'];
  const stageNames = {queued: 'Waiting in queue', preflight: 'Checking prerequisites', preparing: 'Preparing installation media', creating: 'Creating virtual machines', installing_os: 'Install operating system', installing_software: 'Installing software', verifying: 'Verifying services', completed: 'Ready to use', failed: 'Deployment failed', interrupted: 'Deployment interrupted', cleaning: 'Removing deployment resources', cleanup_failed: 'Cleanup needs attention', reverted: 'Resources removed'};
  const statusNames = {queued: 'Queued', running: 'In progress', stopping: 'Stopping', stopped: 'Stopped', completed: 'Completed', failed: 'Failed', interrupted: 'Interrupted', cleaning: 'Cleaning up', cleanup_failed: 'Cleanup failed', reverted: 'Reverted'};
  const failureStatuses = new Set(['failed', 'interrupted', 'cleanup_failed']);
  const busyStatuses = new Set(['queued', 'running', 'stopping', 'cleaning']);
  const stoppableStatuses = new Set(['queued', 'running']);
  const hideableStatuses = new Set(['completed', 'failed', 'interrupted', 'cleanup_failed', 'reverted', 'stopped']);
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
  const endpointIdentity = value => String(value || '').trim().toLowerCase();
  const roleLimits = role => ({cpu: roles[role]?.minCpu || 1, ram: roles[role]?.minRam || ({ubuntu: 2, elasticsearch: 8, kibana: 4, splunk: 4})[role] || 2, disk: roles[role]?.minDisk || 25});
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
      if (response.status === 401 && path !== '/api/login' && state.session === requestSession) showLogin('Your session has expired. Sign in to continue.');
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
  function copyTextFallback(text) {
    const focused = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({length: selection.rangeCount}, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
    const inputSelection = focused && typeof focused.selectionStart === 'number' ? [focused.selectionStart, focused.selectionEnd, focused.selectionDirection] : null;
    const temporary = el('textarea', {readOnly: true, tabindex: '-1', 'aria-label': 'Text to copy', value: text});
    temporary.style.cssText = 'position:fixed;left:-10000px;top:0;width:1px;height:1px;';
    (focused?.closest('dialog[open]') || document.body).append(temporary);
    try {
      temporary.focus({preventScroll: true}); temporary.select(); temporary.setSelectionRange(0, text.length);
      return document.execCommand('copy') === true;
    } catch { return false; }
    finally {
      temporary.remove();
      if (focused?.isConnected) focused.focus({preventScroll: true});
      if (inputSelection && focused?.isConnected) focused.setSelectionRange(...inputSelection);
      if (selection) {
        selection.removeAllRanges();
        for (const range of ranges) if (range.commonAncestorContainer.isConnected) selection.addRange(range);
      }
    }
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
    } catch { /* Browsers may expose the API but deny clipboard permission. */ }
    return copyTextFallback(text);
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
    state.packageTab = 'splunk';
    state.inventory = null;
    state.inventoryHost = null;
    state.deployments = [];
    state.includeHidden = false;
    state.historyRevision++;
    state.visibilityBusy.clear();
    state.stopBusy.clear();
    state.detail = null;
    state.detailPending = null;
    state.detailId = null;
    state.openLogs.clear();
    for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
    $('#wizard-dialog').replaceChildren();
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
    state.detailPending = null;
    state.detailId = null;
    page.replaceChildren(loading('Loading your workspace…'));
    const hash = location.hash.slice(1);
    const setupRoute = /^(?:settings|setup)(?:\/(connection|media|ssh|packages)(?:\/(splunk|fleetmanager))?)?$/.exec(hash);
    if (setupRoute) {
      state.route = 'settings';
      markNav('settings');
      $('#breadcrumb').textContent = 'Setup';
      try {
        const settings = await api('/api/settings');
        if (epoch !== state.routeEpoch) return;
        state.settings = settings;
        if (endpointIdentity(state.inventoryHost) !== endpointIdentity(settings.host)) { state.inventory = null; state.inventoryHost = null; }
        state.setupTab = setupRoute[1] || (settings.configured && !settings.iso_configured ? 'media' : 'connection');
        if (state.setupTab === 'packages') state.packageTab = setupRoute[2] || 'splunk';
        renderSettings();
      } catch (error) { if (epoch === state.routeEpoch) renderLoadError(error, route); }
    } else if (hash.startsWith('deployment/')) {
      state.route = 'detail';
      markNav('deployments');
      $('#breadcrumb').textContent = 'Deployment details';
      try {
        const id = decodeURIComponent(hash.slice('deployment/'.length));
        state.detailId = id;
        const request = ++state.detailRequest;
        const data = await api(`/api/deployments/${encodeURIComponent(id)}`);
        if (epoch !== state.routeEpoch || request !== state.detailRequest) return;
        state.detail = data;
        renderDetail(data);
      } catch (error) { if (epoch === state.routeEpoch) renderLoadError(error, route); }
    } else {
      state.route = 'deployments';
      markNav('deployments');
      $('#breadcrumb').textContent = 'Deployments';
      const results = await Promise.allSettled([api(deploymentListPath()), api('/api/settings')]);
      if (epoch !== state.routeEpoch) return;
      if (results[1].status === 'fulfilled') state.settings = results[1].value;
      if (results[0].status === 'fulfilled') {
        state.deployments = results[0].value;
        renderOverview();
        if (results[1].status === 'rejected') globalError(results[1].reason.message);
        if (hash === 'deployments/continue' && state.wizard?.suspended) openWizard();
      } else renderLoadError(results[0].reason, route);
    }
  }
  function renderLoadError(error, retry) {
    page.replaceChildren(el('div', {class: 'surface empty-state'}, el('div', {class: 'empty-icon'}, icon('alert')), el('h3', {}, 'Couldn’t load this page'), el('p', {}, error.message), button('Try again', '', retry, 'refresh')));
  }
  function heading(title, description, action) {
    return el('div', {class: 'page-heading'}, el('div', {}, el('h1', {}, title), el('p', {}, description)), action);
  }
  function deploymentListPath() { return `/api/deployments${state.includeHidden ? '?include_hidden=true' : ''}`; }
  function stopButton(data, compact = false) {
    if (!stoppableStatuses.has(data.status) && data.status !== 'stopping') return null;
    const control = button(data.status === 'stopping' ? 'Stopping…' : compact ? 'Stop' : 'Stop deployment', `button-small ${compact ? 'button-ghost' : 'button-full'}`, () => openStopDeployment(data), 'close');
    control.id = `deployment-stop-${data.id}`;
    control.disabled = data.status === 'stopping' || state.stopBusy.has(data.id);
    control.setAttribute('aria-label', data.status === 'stopping' ? `Stopping ${data.name}` : `Stop ${data.name}`);
    return control;
  }
  function openStopDeployment(data) {
    const currentData = () => state.detailId === data.id ? state.detailPending || state.detail || data : state.deployments.find(row => row.id === data.id) || data;
    if (!state.session || state.stopBusy.has(data.id) || !stoppableStatuses.has(currentData().status)) return;
    const session = state.session, epoch = state.routeEpoch;
    const dialog = $('#confirm-dialog'), errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const currentView = () => state.session === session && state.routeEpoch === epoch && dialog.open && dialog.contains(confirm);
    const dismiss = button('Keep deploying', '', () => dialog.close());
    const confirm = button('Stop deployment', 'button-primary', async () => {
      if (!currentView() || state.stopBusy.has(data.id)) return;
      if (!stoppableStatuses.has(currentData().status)) { inlineError(errorBox, 'This deployment is no longer queued or running. Close this dialog to see its current status.'); confirm.disabled = true; return; }
      state.stopBusy.add(data.id); state.historyRevision++;
      const existingStop = document.getElementById(`deployment-stop-${data.id}`); if (existingStop) existingStop.disabled = true;
      dialog.dataset.busy = 'true'; dismiss.disabled = true; setBusy(confirm, 'Requesting stop…'); inlineError(errorBox, '');
      try {
        const updated = await api(`/api/deployments/${encodeURIComponent(data.id)}/stop`, {method: 'POST'});
        if (!currentView()) return;
        state.stopBusy.delete(data.id); state.historyRevision++;
        dialog.close();
        const row = {...updated}; delete row.events;
        state.deployments = state.deployments.map(item => item.id === data.id ? row : item);
        if (state.route === 'detail' && state.detailId === data.id) applyDetailUpdate(updated);
        else if (state.route === 'deployments') renderOverview();
        notify(updated.status === 'stopped' ? 'Deployment stopped. Existing VMs and disks are preserved.' : 'Stop requested. GDeploy is waiting for the current operation to finish.');
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally {
        if (state.session === session) { state.stopBusy.delete(data.id); state.historyRevision++; }
        if (dialog.contains(confirm)) { delete dialog.dataset.busy; dismiss.disabled = false; confirm.disabled = false; confirm.replaceChildren('Stop deployment'); }
        if (state.session === session && state.routeEpoch === epoch) {
          const control = document.getElementById(`deployment-stop-${data.id}`);
          if (control) { const replacement = stopButton(currentData(), control.classList.contains('button-ghost')); if (replacement) control.replaceWith(replacement); else control.remove(); }
        }
        if (state.session === session && state.routeEpoch === epoch && state.route === 'detail') refreshDetail();
      }
    }, 'close');
    confirm.id = 'deployment-stop-confirm';
    dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('info'), el('h2', {id: 'confirm-title'}, 'Stop this deployment?')), el('div', {class: 'confirm-body'}, el('p', {}, 'Stop GDeploy’s automation for ', el('strong', {}, data.name), '. Existing VMs, disks, and data stay in place.'), el('p', {}, 'This does not power off VMs or undo work already started. An installation already running inside a VM may continue. GDeploy may finish its current operation before marking this deployment Stopped.'), el('p', {}, 'A stopped deployment cannot be resumed. Delete & redeploy remains a separate action that permanently removes its VMs and disks.'), errorBox, el('div', {class: 'confirm-actions'}, dismiss, confirm)));
    dialog.showModal(); dismiss.focus();
  }
  function visibilityButton(data, compact = false) {
    const hidden = Boolean(data.hidden_at);
    const control = button(hidden ? compact ? 'Restore' : 'Restore to history' : compact ? 'Hide' : 'Hide from history', `button-small ${compact ? 'button-ghost' : 'button-full'}`, () => setDeploymentVisibility(data, !hidden, control));
    control.id = `history-visibility-${data.id}`;
    control.disabled = state.visibilityBusy.has(data.id) || (!hidden && !hideableStatuses.has(data.status));
    control.setAttribute('aria-label', hidden ? `Restore ${data.name} to history` : `Hide ${data.name} from history`);
    control.title = control.disabled ? 'Active deployments cannot be hidden.' : hidden ? 'Show this deployment in the default history view.' : 'Hide this record without changing its VMs, credentials, or logs.';
    return control;
  }
  async function setDeploymentVisibility(data, hidden, control) {
    if (state.visibilityBusy.has(data.id)) return;
    const epoch = state.routeEpoch;
    const session = state.session;
    let updated = null;
    state.visibilityBusy.add(data.id); state.historyRevision++;
    setBusy(control, hidden ? 'Hiding…' : 'Restoring…'); globalError('');
    try {
      updated = await api(`/api/deployments/${encodeURIComponent(data.id)}/visibility`, {method: 'PATCH', body: {hidden}});
      state.visibilityBusy.delete(data.id); state.historyRevision++;
      if (state.session !== session) return;
      if (state.routeEpoch === epoch) {
        if (state.route === 'detail' && state.detailId === data.id) { state.detail = updated; renderDetail(updated); }
        if (state.route === 'deployments') {
          const listEntry = {...updated};
          delete listEntry.events;
          state.deployments = state.deployments.map(row => row.id === data.id ? listEntry : row).filter(row => state.includeHidden || !row.hidden_at);
          renderOverview();
        }
        (document.getElementById(`history-visibility-${data.id}`) || $('#history-show-hidden'))?.focus({preventScroll: true});
      }
      notify(hidden ? 'Deployment hidden. VMs, credentials, and logs are unchanged.' : 'Deployment restored to history.');
    } catch (error) { if (state.routeEpoch === epoch) globalError(error.message); }
    finally {
      state.visibilityBusy.delete(data.id);
      if (state.session === session) {
        const currentControl = document.getElementById(`history-visibility-${data.id}`);
        if (currentControl) {
          const currentData = updated || (state.detail?.id === data.id ? state.detail : state.deployments.find(row => row.id === data.id)) || data;
          const focused = document.activeElement === currentControl;
          const replacement = visibilityButton(currentData, currentControl.classList.contains('button-ghost'));
          currentControl.replaceWith(replacement);
          if (focused) replacement.focus({preventScroll: true});
        }
      }
    }
  }
  function renderOverview() {
    const rows = state.deployments;
    const active = rows.filter(row => busyStatuses.has(row.status)).length;
    const completed = rows.filter(row => row.status === 'completed').length;
    const failures = rows.filter(row => failureStatuses.has(row.status)).length;
    const metric = (title, value, foot, symbol, extra = '') => el('div', {class: `metric ${extra}`}, el('div', {class: 'metric-top'}, title, icon(symbol)), el('div', {class: 'metric-value'}, value), el('div', {class: 'metric-foot'}, foot));
    const metrics = el('div', {class: 'metrics'}, metric('Total deployments', rows.length, state.includeHidden ? 'Including hidden runs' : 'Visible deployment runs', 'grid'), metric('Ready to use', completed, 'Successfully provisioned', 'checkCircle', 'success'), metric('In progress', active, 'Queued, running, stopping, or cleaning', 'activity'), metric('Needs attention', failures, 'Review errors and logs', 'alert', failures ? 'warning' : ''));
    const search = el('input', {class: 'search-field', type: 'search', placeholder: 'Search deployments…', 'aria-label': 'Search deployments', value: state.search, onInput: event => { state.search = event.target.value; renderDeploymentRows(); }});
    const filter = el('select', {class: 'filter-select', 'aria-label': 'Filter by deployment status', onChange: event => { state.filter = event.target.value; renderDeploymentRows(); }}, el('option', {value: 'all'}, 'All statuses'), el('option', {value: 'active'}, 'In progress'), el('option', {value: 'completed'}, 'Completed'), el('option', {value: 'attention'}, 'Needs attention'), el('option', {value: 'stopped'}, 'Stopped'), el('option', {value: 'reverted'}, 'Reverted'));
    filter.value = state.filter;
    const showHidden = el('label', {class: 'check-label history-hidden-filter'}, el('input', {id: 'history-show-hidden', type: 'checkbox', checked: state.includeHidden, onChange: async event => {
      state.includeHidden = event.target.checked; state.historyRevision++;
      await route();
      if (state.route === 'deployments') $('#history-show-hidden')?.focus({preventScroll: true});
    }}), 'Show hidden');
    const history = el('section', {class: 'surface', 'aria-labelledby': 'history-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'history-title'}, 'Deployment history', el('span', {class: 'count-badge'}, rows.length)), el('p', {}, 'Hide finished runs without changing their VMs. Restore them at any time.')), showHidden), el('div', {class: 'history-controls'}, search, filter), el('div', {id: 'deployment-rows'}));
    const setupBanner = !state.settings?.configured || !state.settings?.iso_configured ? el('div', {class: 'connection-banner'}, icon('server'), el('div', {}, el('strong', {}, 'Finish your GDeploy setup'), el('p', {}, 'Connect an ESXi host and select an OS ISO before creating a deployment.')), el('a', {href: state.settings?.configured ? '#settings/media' : '#settings/connection', class: 'button button-small'}, 'Open Setup', icon('arrow'))) : null;
    page.replaceChildren(...[heading('Deployments', 'Build your environment. We’ll take care of the setup.', button(state.wizard?.suspended ? 'Continue deployment' : 'New deployment', 'button-primary', openWizard, 'plus')), metrics, setupBanner, history].filter(Boolean));
    renderDeploymentRows();
  }
  function renderDeploymentRows() {
    const container = $('#deployment-rows');
    if (!container) return;
    if (!state.deployments.length) {
      container.replaceChildren(el('div', {class: 'empty-state'}, el('div', {class: 'empty-icon'}, icon('server')), el('h3', {}, 'No deployments in this view'), el('p', {}, state.includeHidden ? 'Create a deployment to start building your environment.' : 'Create a deployment, or select Show hidden to find and restore hidden records.'), button('Create a deployment', 'button-primary', openWizard, 'plus')));
      return;
    }
    const query = state.search.trim().toLowerCase();
    const rows = state.deployments.filter(row => (!query || String(row.name).toLowerCase().includes(query) || (row.vms || []).some(vm => String(vm.name).toLowerCase().includes(query))) && (state.filter === 'all' || (state.filter === 'active' ? busyStatuses.has(row.status) : state.filter === 'attention' ? failureStatuses.has(row.status) : row.status === state.filter)));
    if (!rows.length) { container.replaceChildren(el('div', {class: 'empty-state'}, el('h3', {}, 'No matching deployments'), el('p', {}, 'Try another name or status filter.'))); return; }
    const body = el('tbody');
    for (const row of rows) {
      const vmRows = row.vms || [];
      body.append(el('tr', {}, el('td', {}, el('a', {class: 'deployment-name', href: `#deployment/${encodeURIComponent(row.id)}`}, row.name), row.hidden_at ? el('span', {class: 'history-hidden-badge'}, 'Hidden') : null, el('div', {class: 'subline'}, vmRows.map(vm => (roles[vm.role] || {short: vm.role}).short).join(' · ') || 'OS deployment')), el('td', {}, statusBadge(row.status)), el('td', {}, `${vmRows.length} ${vmRows.length === 1 ? 'VM' : 'VMs'}`, el('div', {class: 'subline'}, `${vmRows.reduce((n, vm) => n + Number(vm.cpu || 0), 0)} vCPU · ${vmRows.reduce((n, vm) => n + Number(vm.ram_gb || 0), 0)} GB RAM`)), el('td', {}, el('time', {datetime: row.created_at || ''}, date(row.created_at, true))), el('td', {}, el('div', {class: 'history-row-actions'}, stopButton(row, true), visibilityButton(row, true), el('a', {class: 'table-arrow', href: `#deployment/${encodeURIComponent(row.id)}`, 'aria-label': `View ${row.name}`}, icon('arrow'))))));
    }
    container.replaceChildren(el('div', {class: 'table-scroll'}, el('table', {}, el('thead', {}, el('tr', {}, ...['Deployment', 'Status', 'Resources', 'Created', 'Actions'].map(text => el('th', {scope: 'col'}, text)))), body)));
  }
  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return 'Size unavailable';
    const units = ['B', 'KiB', 'MiB', 'GiB'];
    const index = bytes > 0 ? Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024))) : 0;
    return `${new Intl.NumberFormat(undefined, {maximumFractionDigits: index ? 1 : 0}).format(bytes / (1024 ** index))} ${units[index]}`;
  }
  function uploadMedia(file, digest, onProgress, endpoint = '/api/settings/media/upload', algorithm = 'sha256') {
    return new Promise((resolve, reject) => {
      if (!state.session || setupRequired()) { reject(new Error('Sign in with your configured administrator account before uploading media.')); return; }
      const requestSession = state.session;
      const xhr = new XMLHttpRequest();
      const finish = () => { if (state.mediaUpload === xhr) state.mediaUpload = null; };
      xhr.open('POST', `${endpoint}?filename=${encodeURIComponent(file.name)}${digest ? `&${algorithm}=${encodeURIComponent(digest)}` : ''}`);
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
    let storage = null, storageBusy = false, storageRequest = 0;
    let esxiListing = null, browseBusy = false, browseRequest = 0;
    let browseTarget = {datastore: '', folder: ''};
    const currentView = () => panel.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const normalizedHost = value => String(value || '').trim().toLowerCase();
    const sourceLabel = item => ({server: 'GDeploy server', upload: 'Uploaded', esxi: 'ESXi copy'})[item.source] || 'GDeploy server';
    const originLabel = origin => origin ? `${origin.host} · [${origin.datastore}] ${origin.path}` : '';
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const successBox = el('div', {class: 'alert alert-success', role: 'status', hidden: true});
    const storageError = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const storageSummary = el('div', {id: 'media-storage-summary'}, loading('Checking storage…'));
    const storedMediaList = el('div', {class: 'stored-media-list', id: 'stored-media-list'});
    const storageRefresh = button('Refresh storage', 'button-small', () => loadStorage(), 'refresh');
    storageRefresh.id = 'media-storage-refresh';
    const storageSection = el('section', {class: 'media-storage', 'aria-labelledby': 'media-storage-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h3', {id: 'media-storage-title'}, 'Storage & saved ISOs'), el('p', {}, 'Manage uploaded ISOs and copies saved from ESXi.')), storageRefresh), el('div', {class: 'media-storage-body'}, storageError, storageSummary, storedMediaList));
    const selectedSummary = el('div', {class: 'media-selected-summary'});
    const clearSelection = button('Clear saved selection', 'button-small button-ghost', clearMediaSelection);
    clearSelection.id = 'os-media-clear-selection';
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
      const locked = Boolean(busy || externalBusy || storageBusy || !catalog);
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
      storageRefresh.disabled = Boolean(busy || externalBusy || storageBusy);
      clearSelection.disabled = Boolean(locked || !catalog?.has_saved_selection);
      for (const control of storedMediaList.querySelectorAll('button[data-delete-id]')) control.disabled = Boolean(busy || externalBusy || storageBusy || !storage?.items.find(item => item.id === control.dataset.deleteId)?.can_delete);
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
    async function clearMediaSelection() {
      if (!currentView() || busy || externalBusy || storageBusy || !catalog?.has_saved_selection) return;
      busy = 'clear'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true;
      setBusy(clearSelection, 'Clearing…'); syncControls();
      try {
        const next = await api('/api/settings/media', {method: 'DELETE'});
        if (!currentView()) return;
        renderCatalog(next);
        successBox.textContent = next.selected ? 'Saved selection cleared. The server-configured ISO is now selected. Active deployments keep their saved media.' : 'Saved selection cleared. Choose an ISO before creating another deployment. Active deployments keep their saved media.';
        successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally {
        if (currentView()) { await loadStorage(); busy = ''; clearSelection.replaceChildren('Clear saved selection'); onBusy(false); syncControls(); }
      }
    }
    function renderStorage(next) {
      storage = next;
      const disk = next.data_filesystem;
      const percent = disk.total_bytes > 0 ? Math.max(0, Math.min(100, Math.round(disk.used_bytes / disk.total_bytes * 100))) : 0;
      storageSummary.replaceChildren(el('div', {class: 'storage-filesystem'}, el('div', {class: 'storage-heading'}, el('strong', {}, 'GDeploy data filesystem'), el('span', {}, `${percent}% used`)), el('progress', {max: 100, value: percent, 'aria-label': 'Data filesystem space used'}), el('div', {class: 'storage-capacity'}, el('span', {}, el('strong', {}, formatBytes(disk.used_bytes)), ' used'), el('span', {}, el('strong', {}, formatBytes(disk.free_bytes)), ' available'), el('span', {}, `${formatBytes(disk.total_bytes)} total`)), el('p', {class: 'media-help'}, 'Capacity available to GDeploy at ', el('code', {}, disk.path), '. This is the container-visible filesystem and may also contain other host data.')),
        el('dl', {class: 'storage-breakdown'}, el('div', {}, el('dt', {}, 'Saved ISO files'), el('dd', {}, formatBytes(next.managed_iso_bytes))), el('div', {}, el('dt', {}, 'Deployment workspace'), el('dd', {}, formatBytes(next.workspace_bytes)))),
        el('p', {class: 'media-help'}, 'Installation media is built in the deployment workspace. Both saved ISOs and temporary files need free space on this filesystem. Mounted server ISOs are managed on the host.'));
      storedMediaList.replaceChildren(el('h4', {}, 'Saved ISO files', el('span', {class: 'count-badge'}, next.items.length)));
      if (!next.items.length) { storedMediaList.append(el('p', {class: 'media-help'}, 'No uploaded ISOs or ESXi copies are stored yet. Use the source options above to add one.')); return; }
      for (const item of next.items) {
        const reasonId = `media-delete-reason-${item.id}`;
        const reason = item.delete_reason || (!item.available ? item.can_delete ? 'The file is missing. Delete to remove its saved entry.' : 'This saved file is unavailable.' : '');
        const remove = button('Delete', 'button-small button-danger', () => openDeleteMedia(item));
        remove.dataset.deleteId = item.id;
        remove.setAttribute('aria-label', `Delete ${item.name}`);
        if (reason) { remove.setAttribute('aria-describedby', reasonId); remove.title = reason; }
        remove.disabled = !item.can_delete;
        storedMediaList.append(el('article', {class: 'stored-media-item'}, el('div', {class: 'stored-media-info'}, el('div', {class: 'stored-media-name'}, icon('disc'), el('strong', {}, item.name), item.selected ? el('span', {class: 'status status-completed'}, 'Selected') : null), el('p', {class: 'media-help'}, `${formatBytes(item.size_bytes)} · ${sourceLabel(item)}${item.available ? '' : ' · File unavailable'}`), item.origin ? el('p', {class: 'media-origin'}, originLabel(item.origin)) : null, reason ? el('p', {class: 'media-delete-reason', id: reasonId}, reason) : null), remove));
      }
      syncControls();
    }
    async function loadStorage() {
      if (!currentView() || storageBusy) return;
      const request = ++storageRequest;
      storageBusy = true; inlineError(storageError, ''); setBusy(storageRefresh, 'Refreshing…'); syncControls();
      try {
        const next = await api('/api/settings/storage');
        if (currentView() && request === storageRequest) renderStorage(next);
      } catch (error) {
        if (currentView() && request === storageRequest) { inlineError(storageError, error.message); if (!storage) storageSummary.replaceChildren(el('p', {class: 'media-help'}, 'Storage information is unavailable. Refresh to try again.')); }
      } finally {
        if (currentView() && request === storageRequest) { storageBusy = false; storageRefresh.replaceChildren(icon('refresh'), 'Refresh storage'); syncControls(); }
      }
    }
    function openDeleteMedia(item) {
      if (!currentView() || busy || externalBusy || storageBusy || !item.can_delete) return;
      const dialog = $('#confirm-dialog');
      const deleteError = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
      const dismiss = button('Cancel', '', () => dialog.close());
      const remove = button('Delete ISO', 'button-danger', async () => {
        if (!currentView()) { dialog.close(); return; }
        if (busy || externalBusy || storageBusy) return;
        busy = 'delete'; onBusy(true); inlineError(deleteError, ''); setBusy(remove, 'Deleting…'); dismiss.disabled = true;
        dialog.dataset.busy = 'true'; syncControls();
        try {
          const next = await api(`/api/settings/media/${encodeURIComponent(item.id)}`, {method: 'DELETE'});
          if (!currentView()) return;
          renderStorage(next);
          if (catalog) renderCatalog({...catalog, items: catalog.items.filter(candidate => candidate.id !== item.id)});
          dialog.close();
          successBox.textContent = `${item.name} deleted from GDeploy storage.`; successBox.hidden = false;
          notify('Saved ISO deleted.');
        } catch (error) {
          if (currentView()) { inlineError(deleteError, error.message); await loadStorage(); }
        } finally {
          delete dialog.dataset.busy; dismiss.disabled = false; remove.disabled = false; remove.replaceChildren('Delete ISO');
          if (currentView()) { busy = ''; onBusy(false); syncControls(); }
        }
      });
      dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('alert'), el('h2', {id: 'confirm-title'}, 'Delete saved ISO?')), el('div', {class: 'confirm-body'}, el('p', {}, 'Permanently delete ', el('strong', {}, item.name), ` from GDeploy and free ${formatBytes(item.size_bytes)}? You would need to upload or copy it again to use it later.`), item.source === 'esxi' ? el('p', {}, 'The original ISO on ESXi stays unchanged.') : null, el('p', {}, 'Selected media and files used by active deployments cannot be deleted.'), deleteError, el('div', {class: 'confirm-actions'}, dismiss, remove)));
      dialog.showModal(); dismiss.focus();
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
      if (next.has_saved_selection) selectedSummary.append(el('div', {class: 'media-clear-selection'}, clearSelection, el('p', {class: 'media-help'}, 'Clear the saved selection before deleting its ISO. Files are kept until you delete them below. Queued and running deployments keep their media; a server-configured ISO may become the default.')));
      onChange(next);
    }
    async function load() {
      if (!currentView() || busy || externalBusy) return;
      busy = 'load'; inlineError(errorBox, ''); syncControls(); setBusy(refresh, 'Refreshing…');
      try {
        const [catalogResult] = await Promise.allSettled([api('/api/settings/media'), loadStorage()]);
        if (catalogResult.status === 'rejected') throw catalogResult.reason;
        const next = catalogResult.value;
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
      if (!currentView() || busy || externalBusy || storageBusy || !catalog || !form.reportValidity()) return;
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
      finally { if (currentView()) { await loadStorage(); busy = ''; progressBox.hidden = true; onBusy(false); syncControls(); } }
    }}, el('div', {class: 'form-body'}, selectedSummary, errorBox, successBox,
      el('fieldset', {class: 'media-source-options'}, el('legend', {}, 'Choose an ISO source'), el('label', {}, serverRadio, 'GDeploy server'), el('label', {}, esxiRadio, 'ESXi datastore'), el('label', {}, uploadRadio, 'Upload an ISO')),
      serverFields, esxiFields, uploadFields,
      field('Publisher SHA-256 checksum', checksum, el('span', {id: 'os-media-sha256-help'}, 'Copy the checksum from the OS publisher’s download page. GDeploy verifies the file before saving it.')),
      el('p', {class: 'media-compatibility'}, icon('info'), 'Automatic installation currently supports Ubuntu Server 24.04 LTS amd64 using autoinstall. Other ISOs are not supported.'), progressBox), el('div', {class: 'form-footer'}, el('span', {class: 'media-footer-note'}, 'Saved changes apply to new deployments.'), submit));
    const panel = el('section', {class: 'surface', id: 'os-media-panel', 'aria-labelledby': 'os-media-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'os-media-title'}, 'OS installation media'), el('p', {}, 'Choose and verify the ISO used to install your virtual machines.')), badge), form, storageSection);
    $('.surface-header', panel).append(refresh);
    serverSelect.addEventListener('change', () => { checksum.value = currentItem()?.sha256 || ''; successBox.hidden = true; syncControls(); });
    fileInput.addEventListener('change', () => { checksum.value = ''; successBox.hidden = true; inlineError(errorBox, ''); syncControls(); });
    datastoreSelect.addEventListener('change', () => browseESXi(datastoreSelect.value, ''));
    esxiFileSelect.addEventListener('change', () => { checksum.value = ''; successBox.hidden = true; inlineError(errorBox, ''); syncControls(); });
    checksum.addEventListener('input', () => { checksum.value = checksum.value.trim(); });
    return {panel, load, setExternalBusy(value) { externalBusy = value; syncControls(); }};
  }
  function createSplunkPackagePanel(onChange, onBusy) {
    const viewEpoch = state.routeEpoch;
    let catalog = null, mode = 'upload', busy = '', externalBusy = false;
    const currentView = () => panel.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const endpoint = '/api/settings/splunk-package';
    const sourceLabel = item => item.source === 'upload' ? 'Uploaded' : 'GDeploy server';
    const savedChecksum = item => item?.sha512 || item?.sha256 || '';
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const successBox = el('div', {class: 'alert alert-success', role: 'status', hidden: true});
    const badge = el('span', {class: 'status'}, 'Loading…');
    const selectedSummary = el('div', {class: 'media-selected-summary', id: 'splunk-package-summary'});
    const packageList = el('div', {class: 'stored-media-list', id: 'splunk-package-list'});
    const clearSelection = button('Clear saved selection', 'button-small button-ghost', clearPackageSelection);
    clearSelection.id = 'splunk-package-clear-selection';
    const serverSelect = el('select', {id: 'splunk-package-server', name: 'package_id', required: true, 'aria-describedby': 'splunk-package-server-help'});
    const fileInput = el('input', {id: 'splunk-package-file', name: 'package_file', type: 'file', accept: '.tgz', 'aria-describedby': 'splunk-package-file-help'});
    const fileHelp = el('small', {id: 'splunk-package-file-help'}, 'Upload the Linux x86_64 .tgz package from your computer.');
    const checksum = el('input', {id: 'splunk-package-sha256', name: 'checksum', type: 'text', required: true, minlength: 64, maxlength: 128, pattern: '[a-fA-F0-9]{64}([a-fA-F0-9]{64})?', placeholder: 'Paste the 128-character SHA-512 or 64-character SHA-256 checksum', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false', class: 'mono', 'aria-describedby': 'splunk-package-sha256-help'});
    const currentItem = () => catalog?.items?.find(item => item.id === serverSelect.value);
    const sourceInput = value => el('input', {type: 'radio', name: 'splunk-package-source', value, checked: mode === value, onChange: () => {
      mode = value; checksum.value = value === 'server' ? savedChecksum(currentItem()) : '';
      inlineError(errorBox, ''); successBox.hidden = true; syncControls();
    }});
    const uploadRadio = sourceInput('upload'), serverRadio = sourceInput('server');
    const serverFields = el('div', {class: 'media-source-fields', hidden: true}, field('Splunk package on the GDeploy server', serverSelect), el('p', {class: 'media-help', id: 'splunk-package-server-help'}, 'Choose a previous upload or a .tgz file in the media folder beside compose.yaml. Keep its original filename, then refresh this list.'));
    const uploadFields = el('div', {class: 'media-source-fields'}, field('Splunk package file', fileInput), fileHelp);
    const progress = el('progress', {max: 100, value: 0, 'aria-label': 'Splunk package upload progress'});
    const progressText = el('span', {role: 'status', 'aria-live': 'polite'});
    const cancel = button('Cancel upload', 'button-ghost button-small', () => state.mediaUpload?.abort());
    const progressBox = el('div', {class: 'media-progress', hidden: true}, progress, el('div', {}, progressText, cancel));
    function syncControls() {
      const locked = Boolean(busy || externalBusy || !catalog);
      for (const radio of [serverRadio, uploadRadio]) { radio.checked = mode === radio.value; radio.disabled = locked; }
      serverFields.hidden = mode !== 'server'; uploadFields.hidden = mode !== 'upload';
      serverSelect.disabled = locked || mode !== 'server';
      fileInput.disabled = locked || mode !== 'upload'; fileInput.required = mode === 'upload';
      checksum.disabled = locked;
      refresh.disabled = Boolean(busy || externalBusy);
      clearSelection.disabled = Boolean(locked || !catalog?.has_saved_selection);
      for (const control of packageList.querySelectorAll('button[data-delete-id]')) control.disabled = locked || !catalog?.items.find(item => item.id === control.dataset.deleteId)?.can_delete;
      submit.disabled = locked || (mode === 'server' && !serverSelect.value) || (mode === 'upload' && !fileInput.files.length);
      if (!busy) submit.replaceChildren(icon(mode === 'server' ? 'check' : 'layers'), mode === 'upload' ? 'Upload & use package' : 'Use selected package');
      panel.setAttribute('aria-busy', busy ? 'true' : 'false');
    }
    function renderCatalog(next) {
      catalog = next;
      const previousId = serverSelect.value;
      serverSelect.replaceChildren(el('option', {value: ''}, next.items.length ? 'Choose a Splunk package' : 'No .tgz packages found'), ...next.items.filter(item => item.available !== false).map(item => el('option', {value: item.id}, `${item.name} · ${formatBytes(item.size_bytes)} · ${sourceLabel(item)}`)));
      serverSelect.value = next.selected?.id || (next.items.some(item => item.id === previousId && item.available !== false) ? previousId : '');
      if (mode === 'server') checksum.value = savedChecksum(currentItem());
      fileHelp.textContent = `Upload a .tgz file up to ${formatBytes(next.max_upload_bytes)}. Uploads are kept with GDeploy’s persistent data.`;
      badge.className = next.ready ? 'status status-completed' : 'status';
      badge.textContent = next.ready ? 'Ready for preflight' : 'Package required for Splunk';
      if (next.selected) {
        selectedSummary.replaceChildren(el('div', {class: 'media-summary-heading'}, icon('layers'), el('div', {}, el('span', {class: 'eyebrow'}, 'SAVED SPLUNK PACKAGE'), el('strong', {}, next.selected.name), el('p', {}, `${formatBytes(next.selected.size_bytes)} · ${sourceLabel(next.selected)}`))), el('details', {class: 'media-integrity'}, el('summary', {}, `Saved ${next.selected.sha512 ? 'SHA-512' : 'SHA-256'} checksum`), el('code', {class: 'certificate-fingerprint'}, savedChecksum(next.selected) || 'Not configured')));
        if (!next.ready) selectedSummary.append(el('p', {class: 'media-help'}, 'The selected package is unavailable or needs attention. Choose and verify a package below before deploying Splunk.'));
      } else selectedSummary.replaceChildren(el('div', {class: 'media-summary-heading'}, icon('layers'), el('div', {}, el('strong', {}, 'Add the Splunk installer'), el('p', {}, 'Required only when your deployment includes Splunk.'))));
      if (next.has_saved_selection) selectedSummary.append(el('div', {class: 'media-clear-selection'}, clearSelection, el('p', {class: 'media-help'}, 'Clearing the selection keeps the file. Queued and running deployments keep their package; a server-configured package may become the default.')));
      packageList.replaceChildren(el('h4', {}, 'Available packages', el('span', {class: 'count-badge'}, next.items.length)));
      if (!next.items.length) packageList.append(el('p', {class: 'media-help'}, 'No packages saved yet. Upload a Splunk Enterprise package above to get started.'));
      for (const item of next.items) {
        const reasonId = `splunk-package-delete-reason-${item.id}`;
        const reason = item.delete_reason || (item.available === false ? 'This saved file is unavailable.' : '');
        const remove = button('Delete', 'button-small button-danger', () => openDeletePackage(item));
        remove.dataset.deleteId = item.id; remove.disabled = !item.can_delete;
        remove.setAttribute('aria-label', `Delete ${item.name}`);
        if (reason) { remove.setAttribute('aria-describedby', reasonId); remove.title = reason; }
        packageList.append(el('article', {class: 'stored-media-item'}, el('div', {class: 'stored-media-info'}, el('div', {class: 'stored-media-name'}, icon('layers'), el('strong', {}, item.name), item.selected ? el('span', {class: 'status status-completed'}, 'Selected') : null), el('p', {class: 'media-help'}, `${formatBytes(item.size_bytes)} · ${sourceLabel(item)}${item.available === false ? ' · File unavailable' : ''}`), reason ? el('p', {class: 'media-delete-reason', id: reasonId}, reason) : null), remove));
      }
      onChange(next); syncControls();
    }
    async function load() {
      if (!currentView() || busy || externalBusy) return;
      busy = 'load'; inlineError(errorBox, ''); setBusy(refresh, 'Refreshing…'); syncControls();
      try {
        const next = await api(endpoint);
        if (!currentView()) return;
        if (!catalog && next.items.length) mode = 'server';
        renderCatalog(next);
      } catch (error) { if (currentView()) { inlineError(errorBox, error.message); badge.textContent = 'Couldn’t load packages'; } }
      finally { if (currentView()) { busy = ''; refresh.replaceChildren(icon('refresh'), 'Refresh list'); syncControls(); } }
    }
    async function clearPackageSelection() {
      if (!currentView() || busy || externalBusy || !catalog?.has_saved_selection) return;
      busy = 'clear'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true; setBusy(clearSelection, 'Clearing…'); syncControls();
      try {
        const next = await api(endpoint, {method: 'DELETE'});
        if (!currentView()) return;
        renderCatalog(next);
        successBox.textContent = next.selected ? 'Saved selection cleared. The server-configured package is now selected.' : 'Saved selection cleared. Choose a package before creating another Splunk deployment.';
        successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { busy = ''; clearSelection.replaceChildren('Clear saved selection'); onBusy(false); syncControls(); } }
    }
    function openDeletePackage(item) {
      if (!currentView() || busy || externalBusy || !item.can_delete) return;
      const dialog = $('#confirm-dialog');
      const deleteError = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
      const dismiss = button('Cancel', '', () => dialog.close());
      const remove = button('Delete package', 'button-danger', async () => {
        if (!currentView()) { dialog.close(); return; }
        if (busy || externalBusy) return;
        busy = 'delete'; onBusy(true); inlineError(deleteError, ''); setBusy(remove, 'Deleting…'); dismiss.disabled = true; dialog.dataset.busy = 'true'; syncControls();
        try {
          const next = await api(`${endpoint}/${encodeURIComponent(item.id)}`, {method: 'DELETE'});
          if (!currentView()) return;
          renderCatalog(next); dialog.close();
          successBox.textContent = `${item.name} deleted from GDeploy storage.`; successBox.hidden = false;
        } catch (error) {
          if (currentView()) {
            inlineError(deleteError, error.message);
            try { const next = await api(endpoint); if (currentView()) renderCatalog(next); } catch { /* Keep the original deletion error visible. */ }
          }
        } finally {
          delete dialog.dataset.busy; dismiss.disabled = false; remove.disabled = false; remove.replaceChildren('Delete package');
          if (currentView()) { busy = ''; onBusy(false); syncControls(); }
        }
      });
      dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('alert'), el('h2', {id: 'confirm-title'}, 'Delete saved package?')), el('div', {class: 'confirm-body'}, el('p', {}, 'Permanently delete ', el('strong', {}, item.name), ` from GDeploy and free ${formatBytes(item.size_bytes)}? You would need to upload it again to use it later.`), el('p', {}, 'Selected packages and files used by queued or running deployments cannot be deleted.'), deleteError, el('div', {class: 'confirm-actions'}, dismiss, remove)));
      dialog.showModal(); dismiss.focus();
    }
    const refresh = button('Refresh list', 'button-small', load, 'refresh'); refresh.id = 'splunk-package-refresh';
    const submit = button('Upload & use package', 'button-primary', null, 'layers'); submit.type = 'submit';
    const form = el('form', {onSubmit: async event => {
      event.preventDefault();
      if (!currentView() || busy || externalBusy || !catalog || !form.reportValidity()) return;
      const file = fileInput.files[0], uploading = mode === 'upload';
      if (uploading && (!file || !file.name.toLowerCase().endsWith('.tgz') || !file.size || file.size > catalog.max_upload_bytes)) { inlineError(errorBox, `Choose a nonempty .tgz file no larger than ${formatBytes(catalog.max_upload_bytes)}.`); return; }
      busy = uploading ? 'upload' : 'save'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true;
      progressBox.hidden = !uploading; progress.value = 0; cancel.disabled = false; progressText.textContent = 'Starting upload…';
      setBusy(submit, uploading ? 'Uploading package…' : 'Verifying package…'); syncControls();
      try {
        const digest = checksum.value.trim().toLowerCase();
        const algorithm = digest.length === 128 ? 'sha512' : 'sha256';
        const next = uploading ? await uploadMedia(file, digest, (loaded, total) => {
          if (!currentView()) return;
          if (loaded >= total) { progress.removeAttribute('value'); cancel.disabled = true; progressText.textContent = 'Upload complete. Verifying checksum and Splunk package…'; setBusy(submit, 'Verifying package…'); }
          else { progress.value = Math.round(loaded / total * 100); progressText.textContent = `${formatBytes(loaded)} of ${formatBytes(total)} uploaded`; }
        }, `${endpoint}/upload`, algorithm) : await api(endpoint, {method: 'PUT', body: {package_id: serverSelect.value, [algorithm]: digest}});
        if (!currentView()) return;
        mode = 'server'; fileInput.value = ''; renderCatalog(next);
        successBox.textContent = `${next.selected?.name || 'Splunk package'} verified and saved. New Splunk deployments will use this package.`; successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { busy = ''; progressBox.hidden = true; onBusy(false); syncControls(); } }
    }}, el('div', {class: 'form-body'},
      el('div', {class: 'package-download-guide'}, el('p', {}, 'Download the ', el('strong', {}, 'Splunk Enterprise Linux x86_64 .tgz'), ' installer and its SHA-512 checksum from ', el('a', {href: 'https://www.splunk.com/en_us/download/splunk-enterprise.html', target: '_blank', rel: 'noopener noreferrer'}, 'Splunk’s download page', icon('link')), '. Use a package covered by your Splunk license or trial.'), el('p', {class: 'media-help'}, 'GDeploy does not bundle or automatically download Splunk. Upload the package below or select one on the GDeploy server. License acceptance is required when you deploy.')),
      selectedSummary, errorBox, successBox,
      el('fieldset', {class: 'media-source-options'}, el('legend', {}, 'Choose a package source'), el('label', {}, uploadRadio, 'Upload a package'), el('label', {}, serverRadio, 'GDeploy server')),
      uploadFields, serverFields, field('Publisher checksum (SHA-512 or SHA-256)', checksum, el('span', {id: 'splunk-package-sha256-help'}, 'Paste the 128-character hash from Splunk’s .sha512 checksum download for this exact installer. A 64-character SHA-256 checksum is also accepted. GDeploy verifies it before saving.')), progressBox),
      el('div', {class: 'form-footer'}, el('span', {class: 'media-footer-note'}, 'Saved changes apply to new deployments.'), submit));
    const panel = el('section', {class: 'surface', id: 'splunk-package-panel', 'aria-labelledby': 'splunk-package-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'splunk-package-title'}, 'Splunk Enterprise'), el('p', {}, 'Add and manage the installer used for Splunk deployments.')), badge, refresh), form, el('section', {class: 'media-storage'}, el('div', {class: 'media-storage-body'}, packageList)));
    serverSelect.addEventListener('change', () => { checksum.value = savedChecksum(currentItem()); successBox.hidden = true; syncControls(); });
    fileInput.addEventListener('change', () => { checksum.value = ''; successBox.hidden = true; inlineError(errorBox, ''); syncControls(); });
    checksum.addEventListener('input', () => { checksum.value = checksum.value.trim(); });
    return {panel, load, setExternalBusy(value) { externalBusy = value; syncControls(); }};
  }
  function fleetManagerVersionSummary(catalog = {}) {
    if (catalog.mode === 'offline') {
      const selected = catalog.packages?.find(item => item.id === catalog.package_id);
      return selected?.version ? `Version: ${selected.version} from the selected .deb package.` : 'Version: supplied by the selected .deb package.';
    }
    return catalog.online_version ? `Version: ${catalog.online_version} (exact).` : 'Version: latest available in the repository.';
  }
  function createFleetManagerPanel(onChange, onBusy, options = {}) {
    const viewEpoch = state.routeEpoch, endpoint = '/api/settings/fleetmanager';
    let catalog = null, mode = 'online', packageId = '', dependencyIds = new Set(), busy = '', externalBusy = false;
    let versions = [], versionsLoaded = false, versionRequest = 0, versionsPending = false;
    const currentView = () => panel.isConnected && state.session && (options.isCurrent ? options.isCurrent() : state.route === 'settings' && state.routeEpoch === viewEpoch);
    const changed = () => { successBox.hidden = true; options.onDirty?.(); };
    const errorBox = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
    const successBox = el('div', {class: 'alert alert-success', role: 'status', hidden: true});
    const badge = el('span', {class: 'status'}, 'Loading…');
    const savedSummary = el('div', {class: 'media-selected-summary', id: 'fleetmanager-summary'});
    const community = el('input', {id: 'fleetmanager-community', type: 'password', maxlength: 4096, autocomplete: 'new-password', spellcheck: 'false'});
    const communityHelp = el('small');
    const token = el('input', {id: 'fleetmanager-token', type: 'password', maxlength: 4096, autocomplete: 'new-password', spellcheck: 'false'});
    const tokenHelp = el('small');
    const onlineVersion = el('select', {id: 'fleetmanager-online-version', 'aria-describedby': 'fleetmanager-online-version-help fleetmanager-versions-status'}, el('option', {value: ''}, 'Latest available'));
    const onlineVersionHelp = el('small', {id: 'fleetmanager-online-version-help'}, 'Choose Latest available or an exact version from the repository. Availability is checked again when installing on the VM. Loading versions does not save your token or other setup changes.');
    const versionsStatus = el('p', {id: 'fleetmanager-versions-status', class: 'media-help', role: 'status', 'aria-live': 'polite'}, 'Enter or reuse a saved repository token, then load available versions.');
    const versionsError = el('div', {id: 'fleetmanager-versions-error', class: 'alert alert-warning', role: 'alert', hidden: true});
    const refreshVersions = button('Load versions', 'button-small', loadVersions, 'refresh'); refreshVersions.id = 'fleetmanager-load-versions';
    const license = el('input', {id: 'fleetmanager-license', type: 'file', accept: '.pem', 'aria-describedby': 'fleetmanager-license-help'});
    const licenseHelp = el('small', {id: 'fleetmanager-license-help'});
    const sourceRadio = value => el('input', {type: 'radio', name: 'fleetmanager-mode', value, checked: mode === value, onChange: () => { mode = value; changed(); inlineError(errorBox, ''); syncControls(); }});
    const onlineRadio = sourceRadio('online'), offlineRadio = sourceRadio('offline');
    const onlineFields = el('div', {class: 'media-source-fields'}, field('Repository access token', token, tokenHelp), el('p', {class: 'media-help'}, 'Find your token under Downloads → Fleet Manager in the ', el('a', {href: 'https://my.corelight.cloud/', target: '_blank', rel: 'noopener noreferrer'}, 'Corelight customer portal', icon('link')), '. The VM needs internet access to the vendor repository and Ubuntu package repositories. The vendor repository remains configured for future updates.'), field('Version to install', onlineVersion, onlineVersionHelp), refreshVersions, versionsStatus, versionsError);
    const packageSelect = el('select', {id: 'fleetmanager-package', 'aria-describedby': 'fleetmanager-package-help'});
    const dependencyList = el('div', {class: 'fleet-dependencies', id: 'fleetmanager-dependencies'});
    const offlineFields = el('div', {class: 'media-source-fields', hidden: true}, field('FleetManager .deb package', packageSelect, el('span', {id: 'fleetmanager-package-help'}, 'Choose a corelight-fleet package for amd64. Upload it below or place it in the server media folder and refresh.')), el('fieldset', {class: 'fleet-dependency-fieldset'}, el('legend', {}, 'Additional dependency packages'), el('p', {class: 'media-help'}, 'Optional .deb files for dependencies absent from the Ubuntu VM. Supply the full set required by your FleetManager version. Package installation does not fall back to internet repositories.'), dependencyList));
    const uploadFiles = el('input', {id: 'fleetmanager-package-files', type: 'file', accept: '.deb', multiple: true});
    const uploadHelp = el('small');
    const checksum = el('input', {id: 'fleetmanager-package-sha256', type: 'text', maxlength: 64, pattern: '[a-fA-F0-9]{64}', placeholder: 'Optional 64-character SHA-256 checksum', class: 'mono', autocomplete: 'off', spellcheck: 'false'});
    const packageList = el('div', {class: 'stored-media-list', id: 'fleetmanager-package-list'});
    const progress = el('progress', {max: 100, value: 0, 'aria-label': 'FleetManager package upload progress'});
    const progressText = el('span', {role: 'status', 'aria-live': 'polite'});
    const cancel = button('Cancel upload', 'button-ghost button-small', () => state.mediaUpload?.abort());
    const progressBox = el('div', {class: 'media-progress', hidden: true}, progress, el('div', {}, progressText, cancel));
    const clear = button('Clear FleetManager setup', 'button-small button-ghost', () => confirmRemoval()); clear.id = 'fleetmanager-clear';
    function syncControls() {
      const locked = Boolean(busy || externalBusy || !catalog);
      for (const radio of [onlineRadio, offlineRadio]) { radio.checked = radio.value === mode; radio.disabled = locked; }
      onlineFields.hidden = mode !== 'online'; offlineFields.hidden = mode !== 'offline';
      uploads.hidden = mode !== 'offline';
      community.disabled = locked; community.required = !catalog?.community_string_configured;
      token.disabled = locked || mode !== 'online'; token.required = mode === 'online' && !catalog?.repository_token_configured;
      onlineVersion.disabled = locked || mode !== 'online';
      refreshVersions.disabled = locked || mode !== 'online';
      if (busy !== 'versions') refreshVersions.replaceChildren(icon('refresh'), versionsLoaded ? 'Refresh versions' : 'Load versions');
      license.disabled = locked; license.required = !catalog?.license;
      packageSelect.disabled = locked || mode !== 'offline'; packageSelect.required = mode === 'offline';
      for (const input of dependencyList.querySelectorAll('input')) input.disabled = locked || mode !== 'offline';
      uploadFiles.disabled = locked; checksum.disabled = locked || uploadFiles.files.length > 1;
      upload.disabled = locked || !uploadFiles.files.length;
      save.disabled = locked; refresh.disabled = Boolean(busy || externalBusy);
      clear.disabled = locked || (!catalog?.community_string_configured && !catalog?.license && !catalog?.repository_token_configured);
      for (const control of packageList.querySelectorAll('button[data-delete-id]')) control.disabled = locked || !catalog?.packages.find(item => item.id === control.dataset.deleteId)?.can_delete;
      if (!busy) { save.replaceChildren(icon('check'), 'Save FleetManager setup'); upload.replaceChildren(icon('plus'), 'Upload packages'); }
      panel.setAttribute('aria-busy', busy ? 'true' : 'false');
    }
    function renderVersions(selected = onlineVersion.value) {
      const choices = [el('option', {value: ''}, 'Latest available'), ...versions.map(version => el('option', {value: version}, version))];
      if (selected && !versions.includes(selected)) choices.push(el('option', {value: selected}, `${selected} · ${versionsLoaded ? 'selected version not in refreshed list' : 'saved selection · not refreshed'}`));
      onlineVersion.replaceChildren(...choices); onlineVersion.value = selected;
    }
    async function loadVersions() {
      if (!currentView() || busy || externalBusy || !catalog || mode !== 'online') return;
      versionsPending = false;
      const draftToken = token.value, session = state.session, request = ++versionRequest;
      if (!draftToken && !catalog.repository_token_configured) {
        inlineError(versionsError, 'Enter a repository access token to load versions. You can provide the community string and license afterward.'); token.focus(); return;
      }
      if (draftToken && /[^\x21-\x7e]|:/.test(draftToken)) {
        inlineError(versionsError, 'The repository access token must use ASCII characters without whitespace or colons.'); token.focus(); return;
      }
      const currentRequest = () => currentView() && state.session === session && request === versionRequest && mode === 'online' && token.value === draftToken;
      busy = 'versions'; if (options.wizard) onBusy(true); inlineError(versionsError, ''); delete versionsError.dataset.missingVersion; setBusy(refreshVersions, 'Loading versions…'); syncControls();
      try {
        const result = await api(`${endpoint}/versions`, {method: 'POST', body: {repository_token: draftToken}});
        if (!currentRequest()) return;
        if (!Array.isArray(result?.versions) || result.versions.some(version => typeof version !== 'string' || version.length > 128 || !/^(?:[0-9]+:)?[0-9][A-Za-z0-9.+~\-]*$/.test(version))) throw new Error('The repository returned an invalid version list.');
        versions = [...new Set(result.versions)]; versionsLoaded = true; renderVersions();
        versionsStatus.textContent = versions.length ? `${versions.length} repository ${versions.length === 1 ? 'version' : 'versions'} available, newest first.` : 'No exact versions were returned. Latest available and your existing selection are still available.';
        if (onlineVersion.value && !versions.includes(onlineVersion.value)) { versionsError.dataset.missingVersion = 'true'; inlineError(versionsError, 'Your selected version is not in the refreshed repository list. It has been kept; choose an available version or Latest available before deployment.'); }
      } catch (error) {
        if (currentRequest()) { versionsStatus.textContent = versions.length ? 'The version list could not be refreshed. Existing choices may be outdated.' : 'The version list is unavailable. Latest available and your existing selection are still available.'; inlineError(versionsError, `${error.message} Your current selection has been kept. Retry loading versions, or continue with that selection or Latest available.`); }
      } finally {
        if (currentView() && busy === 'versions') { busy = ''; if (options.wizard) onBusy(false); syncControls(); }
      }
    }
    function loadPendingVersions() {
      if (versionsPending && currentView() && !panel.closest('[hidden]') && !busy && !externalBusy && mode === 'online') return loadVersions();
    }
    function renderCatalog(next, resetDraft = false) {
      catalog = next;
      if (resetDraft) { mode = next.mode || 'online'; renderVersions(next.online_version || ''); packageId = next.package_id || ''; dependencyIds = new Set(next.dependency_ids || []); }
      badge.className = next.ready ? 'status status-completed' : 'status'; badge.textContent = next.ready ? 'Ready for preflight' : 'Setup required';
      community.placeholder = next.community_string_configured ? 'Leave blank to keep the saved community string' : 'Community string for your sensor connections';
      communityHelp.textContent = next.community_string_configured ? 'Community string saved. Enter a value only to replace it.' : 'Required for both installation methods. Use printable characters without quotation marks.';
      token.placeholder = next.repository_token_configured ? 'Leave blank to keep the saved repository token' : 'Paste your Corelight repository access token';
      tokenHelp.textContent = next.repository_token_configured ? 'Repository token saved. Enter a value only to replace it.' : 'Required for online installation. This token is stored securely and is not displayed again.';
      licenseHelp.textContent = next.license ? `Saved: ${next.license.name}. Choose a .pem file only to replace it.` : `Required for both methods. Upload the Corelight .pem license file, up to ${formatBytes(next.max_license_bytes)}.`;
      savedSummary.replaceChildren(el('div', {class: 'media-summary-heading'}, icon('server'), el('div', {}, el('span', {class: 'eyebrow'}, 'FLEETMANAGER SETUP'), el('strong', {}, next.ready ? `${next.mode === 'offline' ? 'Offline package' : 'Online repository'} configured` : 'Configure FleetManager before deploying'), el('p', {}, `Community string: ${next.community_string_configured ? 'saved' : 'needed'} · License: ${next.license ? next.license.name : 'needed'}`))));
      savedSummary.append(el('p', {class: 'media-help', id: 'fleetmanager-saved-version'}, fleetManagerVersionSummary(next)));
      if (next.license?.not_after) savedSummary.append(el('p', {class: 'media-help'}, `License expires ${date(next.license.not_after, true)}.`));
      if (next.errors?.length) savedSummary.append(el('ul', {class: 'fleet-readiness-errors'}, next.errors.map(message => el('li', {}, message))));
      if (next.community_string_configured || next.license || next.repository_token_configured) savedSummary.append(clear);
      const packages = next.packages || [];
      const mainPackages = packages.filter(item => item.package === 'corelight-fleet' && item.architecture === 'amd64' && item.available !== false);
      packageSelect.replaceChildren(el('option', {value: ''}, mainPackages.length ? 'Choose a FleetManager package' : 'Upload a corelight-fleet .deb package'), ...mainPackages.map(item => el('option', {value: item.id}, `${item.name} · ${item.version} · ${formatBytes(item.size_bytes)}`))); packageSelect.value = packageId;
      const dependencies = packages.filter(item => item.package !== 'corelight-fleet' && ['amd64', 'all'].includes(item.architecture) && item.available !== false);
      dependencyList.replaceChildren(...dependencies.map(item => el('label', {class: 'check-label fleet-dependency'}, el('input', {type: 'checkbox', checked: dependencyIds.has(item.id), value: item.id, onChange: event => { if (event.target.checked) dependencyIds.add(item.id); else dependencyIds.delete(item.id); changed(); }}), el('span', {}, el('strong', {}, item.name), el('small', {}, `${item.package} ${item.version} · ${item.architecture}`)))));
      if (!dependencies.length) dependencyList.append(el('p', {class: 'media-help'}, 'No dependency packages uploaded. Add any required .deb files below.'));
      uploadHelp.textContent = `Upload one or more .deb files, up to ${formatBytes(next.max_upload_bytes)} each. Files are stored with GDeploy’s persistent data. Select packages and save setup after uploading.`;
      packageList.replaceChildren(el('h4', {}, 'Available .deb packages', el('span', {class: 'count-badge'}, packages.length)));
      if (!packages.length) packageList.append(el('p', {class: 'media-help'}, 'No FleetManager or dependency packages available yet.'));
      for (const item of packages) {
        const reasonId = `fleet-delete-reason-${item.id}`;
        const remove = button('Delete', 'button-small button-danger', () => confirmRemoval(item)); remove.dataset.deleteId = item.id;
        remove.setAttribute('aria-label', `Delete ${item.name}`);
        if (item.delete_reason) { remove.setAttribute('aria-describedby', reasonId); remove.title = item.delete_reason; }
        packageList.append(el('article', {class: 'stored-media-item'}, el('div', {class: 'stored-media-info'}, el('div', {class: 'stored-media-name'}, icon('layers'), el('strong', {}, item.name)), el('p', {class: 'media-help'}, `${item.package} ${item.version} · ${item.architecture} · ${formatBytes(item.size_bytes)} · ${item.source === 'upload' ? 'Uploaded' : 'GDeploy server'}${item.available === false ? ' · File unavailable' : ''}`), el('details', {class: 'media-integrity'}, el('summary', {}, 'Stored SHA-256'), el('code', {class: 'certificate-fingerprint'}, item.sha256)), item.delete_reason ? el('p', {class: 'media-delete-reason', id: reasonId}, item.delete_reason) : null), remove));
      }
      onChange(next); syncControls();
    }
    async function load() {
      if (!currentView() || busy || externalBusy) return;
      let loaded = false;
      busy = 'load'; if (options.wizard) onBusy(true); inlineError(errorBox, ''); setBusy(refresh, 'Refreshing…'); syncControls();
      try { const next = await api(endpoint); if (currentView()) { renderCatalog(next, !catalog); loaded = true; } }
      catch (error) { if (currentView()) { inlineError(errorBox, error.message); badge.textContent = 'Couldn’t load setup'; } }
      finally { if (currentView()) { busy = ''; refresh.replaceChildren(icon('refresh'), 'Refresh'); if (options.wizard) onBusy(false); syncControls(); } }
      if (loaded && mode === 'online' && (token.value || catalog?.repository_token_configured)) { versionsPending = true; await loadPendingVersions(); }
    }
    function confirmRemoval(item = null) {
      if (!currentView() || busy || externalBusy || (item && !item.can_delete)) return;
      const dialog = $('#confirm-dialog'), error = el('div', {class: 'alert alert-error', role: 'alert', hidden: true});
      const dismiss = button('Cancel', '', () => dialog.close());
      const remove = button(item ? 'Delete package' : 'Clear setup', 'button-danger', async () => {
        if (!currentView()) { dialog.close(); return; }
        if (busy || externalBusy) return;
        busy = 'delete'; onBusy(true); dialog.dataset.busy = 'true'; dismiss.disabled = true; setBusy(remove, 'Removing…'); syncControls();
        try {
          const next = await api(item ? `${endpoint}/packages/${encodeURIComponent(item.id)}` : endpoint, {method: 'DELETE'});
          if (!currentView()) return;
          if (item) { if (packageId === item.id) packageId = ''; dependencyIds.delete(item.id); }
          else { community.value = ''; token.value = ''; license.value = ''; versions = []; versionsLoaded = false; versionsPending = false; versionRequest++; inlineError(versionsError, ''); versionsStatus.textContent = 'Enter or reuse a saved repository token, then load available versions.'; }
          renderCatalog(next, !item); dialog.close(); successBox.textContent = item ? `${item.name} deleted.` : 'FleetManager setup cleared. Uploaded packages and existing deployments are preserved.'; successBox.hidden = false;
        } catch (failure) { if (currentView()) inlineError(error, failure.message); }
        finally { delete dialog.dataset.busy; dismiss.disabled = false; remove.disabled = false; remove.replaceChildren(item ? 'Delete package' : 'Clear setup'); if (currentView()) { busy = ''; onBusy(false); syncControls(); } }
      });
      dialog.replaceChildren(el('div', {class: 'confirm-head'}, icon('alert'), el('h2', {id: 'confirm-title'}, item ? 'Delete saved package?' : 'Clear FleetManager setup?')), el('div', {class: 'confirm-body'}, el('p', {}, item ? `Permanently delete ${item.name} and free ${formatBytes(item.size_bytes)}? Selected packages and files used by queued or running deployments are protected.` : 'Remove the saved community string, repository token, license, and package selections for future deployments. Existing deployments and uploaded packages are preserved.'), error, el('div', {class: 'confirm-actions'}, dismiss, remove)));
      dialog.showModal(); dismiss.focus();
    }
    const refresh = button('Refresh', 'button-small', load, 'refresh'); refresh.id = 'fleetmanager-refresh';
    const save = button('Save FleetManager setup', 'button-primary', null, 'check'); save.type = 'submit';
    save.hidden = Boolean(options.wizard);
    async function saveSettings() {
      if (!currentView() || busy || externalBusy || !catalog || !form.reportValidity()) return false;
      if (community.value && /["'\x00-\x1f\x7f]/.test(community.value)) { inlineError(errorBox, 'Use a community string without quotation marks or control characters.'); community.focus(); return false; }
      if (mode === 'online' && token.value && /[^\x21-\x7e]|:/.test(token.value)) { inlineError(errorBox, 'The repository access token must use ASCII characters without whitespace or colons.'); token.focus(); return false; }
      const file = license.files[0];
      if (file && (!file.name.toLowerCase().endsWith('.pem') || !file.size || file.size > catalog.max_license_bytes)) { inlineError(errorBox, `Choose a nonempty .pem license file no larger than ${formatBytes(catalog.max_license_bytes)}.`); license.focus(); return false; }
      busy = 'save'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true; setBusy(save, 'Saving…'); syncControls();
      try {
        const payload = {mode, online_version: mode === 'online' ? onlineVersion.value.trim() : '', package_id: packageId || null, dependency_ids: [...dependencyIds]};
        if (community.value) payload.community_string = community.value;
        if (mode === 'online' && token.value) payload.repository_token = token.value;
        if (file) { payload.license_pem = await file.text(); payload.license_name = file.name; }
        if (!currentView()) return false;
        const next = await api(endpoint, {method: 'PUT', body: payload});
        if (!currentView()) return false;
        community.value = ''; token.value = ''; license.value = ''; renderCatalog(next, true);
        successBox.textContent = 'FleetManager setup saved. New FleetManager deployments will use this configuration.'; successBox.hidden = false;
        return next.ready;
      } catch (error) { if (currentView()) { inlineError(errorBox, error.message); errorBox.scrollIntoView({block: 'center'}); } return false; }
      finally { if (currentView()) { busy = ''; onBusy(false); syncControls(); } }
    }
    const form = el('form', {onSubmit: event => { event.preventDefault(); if (options.onContinue) options.onContinue(); else saveSettings(); }}, el('div', {class: 'form-body'}, savedSummary, errorBox, successBox,
      el('fieldset', {class: 'media-source-options'}, el('legend', {}, 'Installation method'), el('label', {}, onlineRadio, 'Online repository'), el('label', {}, offlineRadio, 'Offline package')),
      field('Community string', community, communityHelp), field('Corelight license (.pem)', license, licenseHelp), onlineFields, offlineFields),
      el('div', {class: 'form-footer'}, el('span', {class: 'media-footer-note'}, options.wizard ? 'Save & continue validates these settings and saves them as defaults for new deployments.' : 'Saved changes apply to new deployments.'), save));
    const upload = button('Upload packages', 'button-primary button-small', async () => {
      if (!currentView() || busy || externalBusy || !catalog) return;
      const files = [...uploadFiles.files];
      if (!files.length || files.some(file => !file.name.toLowerCase().endsWith('.deb') || !file.size || file.size > catalog.max_upload_bytes)) { inlineError(errorBox, `Choose nonempty .deb files no larger than ${formatBytes(catalog.max_upload_bytes)} each.`); return; }
      if (files.length === 1 && !checksum.reportValidity()) return;
      const expected = files.length === 1 ? checksum.value.trim().toLowerCase() : '';
      busy = 'upload'; onBusy(true); inlineError(errorBox, ''); successBox.hidden = true; progressBox.hidden = false; progress.value = 0; cancel.disabled = false; setBusy(upload, 'Uploading…'); syncControls();
      try {
        for (const [index, file] of files.entries()) {
          const next = await uploadMedia(file, expected, (loaded, total) => { if (currentView()) { progress.value = Math.round((index + loaded / total) / files.length * 100); progressText.textContent = `${file.name}: ${formatBytes(loaded)} of ${formatBytes(total)}${loaded >= total ? ' · Validating package…' : ''}`; } }, `${endpoint}/packages/upload`);
          if (!currentView()) return;
          const item = next.packages.find(candidate => candidate.id === next.uploaded_package_id);
          if (item?.package === 'corelight-fleet') packageId = item.id; else if (item) dependencyIds.add(item.id);
          renderCatalog(next);
        }
        uploadFiles.value = ''; checksum.value = ''; successBox.textContent = 'Packages uploaded. Select Offline package and save FleetManager setup to use them for new deployments.'; successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { busy = ''; progressBox.hidden = true; onBusy(false); syncControls(); } }
    }, 'plus'); upload.id = 'fleetmanager-upload';
    const uploads = el('section', {class: 'media-storage'}, el('div', {class: 'surface-header'}, el('div', {}, el('h3', {}, 'Offline packages'), el('p', {}, 'Upload FleetManager and any required dependency .deb files.'))), el('div', {class: 'media-storage-body'}, field('Package files (.deb)', uploadFiles, uploadHelp), field('Expected SHA-256 · optional', checksum, 'Only for a single file. If supplied by your package source, GDeploy checks this value. Otherwise it records a checksum for future integrity checks.'), el('div', {class: 'fleet-upload-actions'}, upload), progressBox, packageList));
    const panel = el('section', {class: 'surface', id: 'fleetmanager-panel', 'aria-labelledby': 'fleetmanager-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'fleetmanager-title'}, 'FleetManager'), el('p', {}, 'Configure Corelight FleetManager installation and sensor access.')), badge, refresh), form, uploads);
    packageSelect.addEventListener('change', () => { packageId = packageSelect.value; changed(); });
    uploadFiles.addEventListener('change', () => { checksum.value = ''; syncControls(); });
    onlineVersion.addEventListener('change', () => { changed(); inlineError(errorBox, ''); if (versionsError.dataset.missingVersion && (!onlineVersion.value || versions.includes(onlineVersion.value))) { inlineError(versionsError, ''); delete versionsError.dataset.missingVersion; } });
    token.addEventListener('input', () => { versionRequest++; versions = []; versionsLoaded = false; renderVersions(); inlineError(versionsError, ''); versionsStatus.textContent = 'Repository token changed. Load versions to refresh the choices for this token.'; syncControls(); });
    for (const input of [community, token, license]) input.addEventListener('input', () => { changed(); inlineError(errorBox, ''); });
    return {panel, load, save: saveSettings, activate() { if (versionsPending) queueMicrotask(loadPendingVersions); }, canSave: () => Boolean(catalog && !busy && !externalBusy), setExternalBusy(value) { externalBusy = value; syncControls(); if (!value && versionsPending) queueMicrotask(loadPendingVersions); }};
  }
  function createSSHAccessPanel(onSaved, onBusy, initialDraft) {
    const viewEpoch = state.routeEpoch;
    let savedKeys = [], loaded = false, busy = false, externalBusy = false;
    const removalButtons = new Map();
    const keyIdentity = value => value.trim().split(/\s+/).slice(0, 2).join(' ');
    const savedText = () => savedKeys.map(key => key.public_key).join('\n');
    const draftLines = () => editor.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const changed = () => draftLines().join('\n') !== savedText();
    const currentView = () => panel.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const errorBox = el('div', {id: 'ssh-keys-error', class: 'alert alert-error', role: 'alert', hidden: true});
    const successBox = el('div', {id: 'ssh-keys-success', class: 'alert alert-success', role: 'status', hidden: true});
    const badge = el('span', {class: 'status'}, 'Optional');
    const editor = el('textarea', {id: 'ssh-public-keys', rows: 7, maxlength: 819249, spellcheck: 'false', autocomplete: 'off', autocapitalize: 'none', 'aria-describedby': 'ssh-public-keys-help ssh-keys-scope', placeholder: 'ssh-ed25519 AAAA… your-name@your-computer', onInput: () => { inlineError(errorBox, ''); successBox.hidden = true; syncControls(); }});
    const savedList = el('ul', {class: 'ssh-key-list', id: 'ssh-saved-keys', 'aria-label': 'Saved SSH public keys'});
    const savedSummary = el('p', {class: 'ssh-key-summary'});
    const saveNote = el('span', {class: 'media-footer-note'});
    const retry = button('Retry loading keys', 'button-small', () => load(), 'refresh'); retry.hidden = true;
    const discard = button('Discard changes', 'button-ghost', () => { editor.value = savedText(); inlineError(errorBox, ''); successBox.hidden = true; syncControls(); });
    const save = button('Save SSH keys', 'button-primary', null, 'check'); save.type = 'submit';
    function syncControls() {
      const locked = busy || externalBusy;
      const dirty = loaded && changed();
      const clearing = savedKeys.length > 0 && !draftLines().length;
      editor.disabled = !loaded || locked;
      save.disabled = !loaded || locked || !dirty;
      discard.disabled = !loaded || locked || !dirty;
      retry.disabled = locked;
      if (!busy) save.replaceChildren(icon('check'), clearing ? 'Save & remove all keys' : 'Save SSH keys');
      saveNote.textContent = clearing ? 'Saving removes all administrator public keys from future deployments.' : dirty ? 'Unsaved changes. Save to apply them to future deployments.' : 'Saved changes apply to future deployments.';
      const identities = new Set(draftLines().map(keyIdentity));
      for (const [identity, control] of removalButtons) {
        const included = identities.has(identity);
        control.disabled = !loaded || locked || !included;
        control.textContent = included ? 'Remove from draft' : 'Removal pending';
      }
      panel.setAttribute('aria-busy', String(busy));
    }
    function renderSaved() {
      removalButtons.clear(); savedList.replaceChildren();
      badge.textContent = savedKeys.length ? `${savedKeys.length} ${savedKeys.length === 1 ? 'key' : 'keys'} saved` : 'Optional';
      badge.className = savedKeys.length ? 'status status-completed' : 'status';
      savedSummary.textContent = savedKeys.length ? 'These keys are currently saved. Edit the text above or remove a key from the draft, then save your changes.' : 'No administrator public keys saved. This optional step does not block deployment.';
      for (const key of savedKeys) {
        const identity = keyIdentity(key.public_key);
        const remove = button('Remove from draft', 'button-small button-ghost', () => {
          editor.value = draftLines().filter(line => keyIdentity(line) !== identity).join('\n');
          inlineError(errorBox, ''); successBox.hidden = true; syncControls(); editor.focus();
        });
        remove.setAttribute('aria-label', `Remove public key ${key.fingerprint} from draft`);
        removalButtons.set(identity, remove);
        savedList.append(el('li', {class: 'ssh-key-item'}, el('div', {class: 'ssh-key-info'}, el('strong', {}, key.comment || 'No comment'), el('span', {class: 'ssh-key-type'}, key.type), el('code', {class: 'ssh-key-fingerprint'}, key.fingerprint)), remove));
      }
    }
    async function load() {
      if (!currentView() || busy) return;
      busy = true; loaded = false; retry.hidden = true;
      inlineError(errorBox, ''); savedSummary.textContent = 'Loading saved public keys…';
      onBusy(true); syncControls();
      try {
        const result = await api('/api/settings/ssh-keys');
        if (!currentView()) return;
        savedKeys = result.keys; loaded = true; editor.value = initialDraft === undefined ? savedText() : initialDraft;
        initialDraft = undefined;
        renderSaved(); onSaved(result.count);
      } catch (error) {
        if (currentView()) { inlineError(errorBox, error.message); savedSummary.textContent = 'Saved keys could not be loaded. Retry before making changes.'; retry.hidden = false; }
      } finally {
        if (currentView()) { busy = false; onBusy(false); syncControls(); }
      }
    }
    const form = el('form', {onSubmit: async event => {
      event.preventDefault();
      if (!currentView() || !loaded || busy || externalBusy || !changed()) return;
      inlineError(errorBox, ''); successBox.hidden = true;
      const lines = draftLines();
      if (/-----BEGIN[^\r\n]*PRIVATE KEY-----/i.test(editor.value)) {
        inlineError(errorBox, 'Paste only SSH public keys from .pub files. Keep private keys on your own device; private keys must not be sent to GDeploy.'); editor.focus(); return;
      }
      if (lines.length > 50) { inlineError(errorBox, 'Save up to 50 SSH public keys, one complete key per line.'); editor.focus(); return; }
      busy = true; onBusy(true); setBusy(save, 'Saving keys…'); syncControls();
      try {
        const result = await api('/api/settings/ssh-keys', {method: 'PUT', body: {public_keys: lines}});
        if (!currentView()) return;
        savedKeys = result.keys; editor.value = savedText(); renderSaved(); onSaved(result.count);
        successBox.textContent = result.count ? `${result.count} SSH public ${result.count === 1 ? 'key saved' : 'keys saved'}. They will be installed for gdeploy on newly queued deployments.` : 'Administrator public keys cleared for future deployments. Existing and already queued VMs are unchanged.';
        successBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { busy = false; onBusy(false); syncControls(); } }
    }}, el('div', {class: 'form-body'}, errorBox, successBox,
      el('p', {class: 'ssh-access-intro'}, 'Add your public SSH keys to sign in as ', el('code', {}, 'gdeploy'), ' on each new virtual machine. Keep the matching private keys on your own devices.'),
      field('SSH public keys', editor, el('span', {id: 'ssh-public-keys-help'}, 'Paste one complete .pub key per line, up to 50 keys. Supported: Ed25519, RSA (2048 bits or larger), and ECDSA. Do not paste private keys.')),
      el('p', {class: 'ssh-keys-scope', id: 'ssh-keys-scope'}, 'Changes apply only to deployments queued after you save. Existing and already queued VMs keep their current keys. Password access and GDeploy’s own automation key remain available.'), retry),
      el('div', {class: 'form-footer'}, saveNote, el('div', {class: 'ssh-save-actions'}, discard, save)));
    const panel = el('section', {class: 'surface', id: 'ssh-access-panel', 'aria-labelledby': 'ssh-access-title'},
      el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'ssh-access-title'}, 'SSH access'), el('p', {}, 'Optional public keys for administrator access to new VMs.')), badge), form,
      el('section', {class: 'ssh-saved-section', 'aria-labelledby': 'ssh-saved-title'}, el('h3', {id: 'ssh-saved-title'}, 'Saved public keys'), savedSummary, savedList),
      el('section', {class: 'ssh-help-section', 'aria-labelledby': 'ssh-connect-title'}, el('h3', {id: 'ssh-connect-title'}, 'Connect from your computer'),
        el('p', {}, 'Use an existing public key from a ', el('code', {}, '.pub'), ' file, or create a key pair on your computer with ', el('code', {}, 'ssh-keygen -t ed25519'), '. Paste only the public key above.'),
        el('p', {}, 'After deployment, connect using the matching private key:'), el('pre', {}, el('code', {}, 'ssh -i ~/.ssh/id_ed25519 gdeploy@VM_IP')),
        el('p', {}, 'Replace the private-key path and VM_IP with your values. Find the VM’s IP address in its deployment details.')));
    return {panel, load, getDraft() { return loaded && changed() ? editor.value : undefined; }, setExternalBusy(value) { externalBusy = value; syncControls(); }};
  }
  function renderSettings(initialSSHdraft) {
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
    let defaultsBusy = '', defaultsEdited = false, defaultDraft = '';
    let deploymentDefaults = settings.deployment_defaults || {host: settings.host || null, default_network: null, applies_to_host: true};
    let mediaBusy = false;
    let mediaControls = null;
    let sshBusy = false;
    let sshControls = null;
    let packagesBusy = false;
    let packagesControls = null;
    let fleetBusy = false;
    let fleetControls = null;
    const returnToWizard = state.wizard?.suspended ? button('Return to deployment', 'button-primary button-small', () => { location.hash = 'deployments/continue'; }, 'back') : null;
    let fingerprintConfirmed = false;
    let trustButton = null;
    let removeButton = null;
    let comparison = null;
    const currentView = () => form.isConnected && state.session && state.route === 'settings' && state.routeEpoch === viewEpoch;
    const connectionInventory = () => state.inventory && hostKey(state.inventoryHost) === hostKey(settings.host) ? state.inventory : null;
    const defaultsApply = () => deploymentDefaults.applies_to_host === true;
    const defaultNetwork = el('select', {id: 'deployment-default-network', 'aria-describedby': 'deployment-default-network-help', onChange: () => { defaultsEdited = true; defaultDraft = defaultNetwork.value; defaultsSuccess.hidden = true; inlineError(defaultsError, ''); syncControls(); }});
    const defaultNetworkHelp = el('small', {id: 'deployment-default-network-help'});
    const defaultsStatus = el('p', {id: 'deployment-default-status', class: 'media-help'});
    const defaultsWarning = el('div', {id: 'deployment-default-warning', class: 'alert alert-warning', role: 'status', hidden: true});
    const defaultsError = el('div', {id: 'deployment-default-error', class: 'alert alert-error', role: 'alert', hidden: true});
    const defaultsSuccess = el('div', {id: 'deployment-default-success', class: 'alert alert-success', role: 'status', hidden: true});
    const saveDefault = button('Save default', 'button-primary button-small', () => saveDeploymentDefault(false), 'check'); saveDefault.id = 'deployment-default-save';
    const clearDefault = button('Clear default', 'button-small button-ghost', () => saveDeploymentDefault(true)); clearDefault.id = 'deployment-default-clear';
    const defaultsPanel = el('section', {id: 'deployment-defaults-panel', class: 'surface'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {}, 'Deployment defaults'), el('p', {}, 'Choose the port group used for new VM configurations. Each VM can still use a different port group.'))), el('div', {class: 'form-body'}, defaultsStatus, defaultsWarning, defaultsError, defaultsSuccess, field('Default port group', defaultNetwork, defaultNetworkHelp)), el('div', {class: 'form-footer'}, el('span', {class: 'media-footer-note'}, 'Existing jobs and VM choices in your current draft stay unchanged.'), el('div', {class: 'defaults-actions'}, clearDefault, saveDefault)));
    function renderNetworkDefaults() {
      const inventory = connectionInventory(), saved = deploymentDefaults.default_network || '';
      const networks = (inventory?.networks || []).map(item => item.name);
      const count = name => networks.filter(item => item === name).length;
      const chosen = defaultsEdited ? defaultDraft : defaultsApply() ? saved : '';
      defaultNetwork.replaceChildren(el('option', {value: ''}, inventory ? 'Choose a default port group' : 'Test connection to load port groups'), ...[...new Set(networks)].map(name => el('option', {value: name, disabled: count(name) !== 1}, count(name) === 1 ? name : `${name} · ambiguous name`)));
      defaultNetwork.value = count(chosen) === 1 ? chosen : '';
      defaultsStatus.textContent = saved ? `Saved default: ${saved} · ESXi host: ${deploymentDefaults.host || 'unavailable'}` : 'No default saved. New VMs require an explicit port group selection.';
      defaultNetworkHelp.textContent = !settings.configured ? 'Save your ESXi connection first, then test it to load port groups.' : !inventory ? 'Test the saved connection above to load its available port groups.' : 'This default is tied to the saved ESXi host and is checked again when you create a deployment.';
      const warning = saved && !defaultsApply() ? 'This default belongs to a different ESXi host. Choose a port group on the current host and save a new default, or clear the saved default.' : saved && inventory && count(saved) > 1 ? 'The saved default port group name is ambiguous on this ESXi host. Choose a group with a unique name or clear the default.' : saved && inventory && !networks.includes(saved) ? 'The saved default port group is not available on this ESXi host. Choose another port group or clear the default. New VMs will require a selection.' : chosen && inventory && count(chosen) !== 1 ? 'Your selected port group is unavailable or ambiguous. Choose a port group with a unique name from the refreshed list.' : '';
      inlineError(defaultsWarning, warning);
    }
    async function saveDeploymentDefault(clear) {
      if (!currentView() || defaultsBusy || (clear ? clearDefault.disabled : saveDefault.disabled)) return;
      const session = state.session, expectedHost = settings.host, selected = defaultNetwork.value;
      defaultsBusy = clear ? 'clear' : 'save'; inlineError(defaultsError, ''); defaultsSuccess.hidden = true;
      setBusy(clear ? clearDefault : saveDefault, clear ? 'Clearing…' : 'Saving…'); syncControls();
      try {
        const next = await api('/api/settings/deployment-defaults', clear ? {method: 'DELETE'} : {method: 'PUT', body: {host: expectedHost, default_network: selected}});
        if (!currentView() || state.session !== session || hostKey(state.settings?.host) !== hostKey(expectedHost)) return;
        deploymentDefaults = next; settings.deployment_defaults = next; state.settings.deployment_defaults = next; defaultsEdited = false;
        renderNetworkDefaults(); invalidatePreflight();
        defaultsSuccess.textContent = clear ? 'Default cleared. Choose a port group for each new VM.' : 'Default port group saved. It applies to new VM configurations; existing jobs and draft VM choices are unchanged.';
        defaultsSuccess.hidden = false;
      } catch (error) { if (currentView() && state.session === session) inlineError(defaultsError, error.message); }
      finally { if (currentView() && state.session === session) { defaultsBusy = ''; clearDefault.replaceChildren('Clear default'); saveDefault.replaceChildren(icon('check'), 'Save default'); syncControls(); } }
    }
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
      const busy = Boolean(connectionBusy || certificateBusy || defaultsBusy || mediaBusy || sshBusy || packagesBusy || fleetBusy);
      const identityChanged = hostKey(host.value) !== hostKey(settings.host) || username.value.trim() !== (settings.username || '');
      const lockInputs = Boolean(connectionBusy || defaultsBusy || ['trust', 'remove'].includes(certificateBusy));
      host.disabled = lockInputs; username.disabled = lockInputs; password.disabled = lockInputs;
      password.required = !settings.configured || identityChanged;
      save.disabled = busy;
      test.disabled = busy || !settings.configured || identityChanged || Boolean(password.value);
      retrieve.disabled = busy || !host.value.trim();
      defaultNetwork.disabled = busy || !settings.configured || !connectionInventory() || identityChanged || Boolean(password.value);
      saveDefault.disabled = defaultNetwork.disabled || !defaultNetwork.value || (connectionInventory()?.networks || []).filter(network => network.name === defaultNetwork.value).length !== 1;
      clearDefault.disabled = busy || !deploymentDefaults.default_network;
      defaultsPanel.setAttribute('aria-busy', defaultsBusy ? 'true' : 'false');
      if (trustButton) trustButton.disabled = busy || !fingerprintConfirmed || !certificateUsable();
      if (removeButton) removeButton.disabled = busy;
      if (comparison) comparison.disabled = busy || !certificateUsable();
      certificatePanel.setAttribute('aria-busy', certificateBusy ? 'true' : 'false');
      mediaControls?.setExternalBusy(Boolean(connectionBusy || certificateBusy || defaultsBusy || sshBusy || packagesBusy || fleetBusy));
      sshControls?.setExternalBusy(Boolean(connectionBusy || certificateBusy || defaultsBusy || mediaBusy || packagesBusy || fleetBusy));
      packagesControls?.setExternalBusy(Boolean(connectionBusy || certificateBusy || defaultsBusy || mediaBusy || sshBusy || fleetBusy));
      fleetControls?.setExternalBusy(Boolean(connectionBusy || certificateBusy || defaultsBusy || mediaBusy || sshBusy || packagesBusy));
      if (returnToWizard) returnToWizard.disabled = busy;
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
            state.inventory = null; state.inventoryHost = null; renderNetworkDefaults();
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
            state.inventory = null; state.inventoryHost = null; renderNetworkDefaults();
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
      state.inventory = null; state.inventoryHost = null; renderNetworkDefaults(); renderReadiness();
      try {
        const inventory = await api('/api/inventory');
        if (!currentView()) return;
        state.inventory = inventory; state.inventoryHost = settings.host;
        renderNetworkDefaults(); renderReadiness();
        resultBox.className = 'alert alert-success';
        resultBox.replaceChildren(el('strong', {}, `Connected to ${inventory.host?.name || settings.host}`), el('div', {class: 'inventory-summary'}, el('span', {}, `${inventory.host?.cpu_threads || 0} CPU threads`), el('span', {}, `${inventory.host?.memory_gb || 0} GB memory`), el('span', {}, `${inventory.datastores?.length || 0} datastores`), el('span', {}, `${inventory.networks?.length || 0} networks`)));
        resultBox.hidden = false;
      } catch (error) { if (currentView()) inlineError(errorBox, error.message); }
      finally { if (currentView()) { connectionBusy = ''; test.replaceChildren(icon('refresh'), 'Test saved connection'); syncControls(); } }
    }, 'refresh');
    const form = el('form', {class: 'surface', id: 'esxi-connection-panel', onSubmit: async event => {
      event.preventDefault();
      if (!currentView() || connectionBusy || certificateBusy || defaultsBusy || mediaBusy || sshBusy || !form.reportValidity()) return;
      const payload = {host: host.value.trim(), username: username.value.trim(), password: password.value, verify_tls: true};
      inlineError(errorBox, ''); resultBox.hidden = true; connectionBusy = 'save'; setBusy(save, 'Saving…'); syncControls();
      try {
        await api('/api/settings', {method: 'PUT', body: payload});
        if (!currentView()) return;
        password.value = '';
        const updated = await api('/api/settings');
        if (!currentView()) return;
        state.settings = updated; state.inventory = null; state.inventoryHost = null;
        renderSettings(sshControls?.getDraft());
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
      if (updateHash) history.replaceState(history.state, '', `#settings/${key}${key === 'packages' && state.packageTab !== 'splunk' ? `/${state.packageTab}` : ''}`);
      if (focus) tabs[key].button.focus();
      fleetControls?.activate();
    }
    function createSetupTab(key, number, title) {
      const marker = el('span', {class: 'setup-step-number', 'aria-hidden': 'true'}, number);
      const description = el('small');
      const tab = el('button', {type: 'button', class: 'setup-step', id: `setup-tab-${key}`, role: 'tab', 'aria-controls': `setup-panel-${key}`, 'aria-selected': 'false', tabindex: '-1', onClick: () => selectSetupTab(key), onKeydown: event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const names = Object.keys(tabs);
        const direction = ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1;
        const next = event.key === 'Home' ? names[0] : event.key === 'End' ? names[names.length - 1] : names[(names.indexOf(key) + direction + names.length) % names.length];
        selectSetupTab(next, {focus: true});
      }}, marker, el('span', {}, el('strong', {}, title), description));
      tabs[key] = {button: tab, marker, description, number};
      steps.append(tab);
    }
    createSetupTab('connection', '1', 'ESXi connection');
    createSetupTab('media', '2', 'OS installation media');
    createSetupTab('ssh', '3', 'SSH access');
    createSetupTab('packages', '4', 'Software packages');
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
      const sshCount = Number(saved.ssh_key_count || 0);
      updateTab('ssh', sshCount > 0, sshCount ? `${sshCount} public ${sshCount === 1 ? 'key' : 'keys'} saved · optional` : 'Optional · add your public keys');
      const configuredSoftware = Number(Boolean(saved.splunk_configured)) + Number(Boolean(saved.fleetmanager_configured));
      updateTab('packages', configuredSoftware > 0, configuredSoftware ? `${configuredSoftware} software ${configuredSoftware === 1 ? 'configuration' : 'configurations'} ready` : 'Set up the software you plan to deploy');
      readiness.replaceChildren(el('div', {class: 'surface-header'}, el('h2', {}, 'Setup checklist')), el('div', {class: 'setup-list'}, item(saved.configured, 'ESXi host', saved.configured ? state.inventory ? 'Connection tested successfully.' : 'Connection details saved. Test the connection to verify access.' : 'Save your host credentials to load inventory.', 'server'), item(saved.iso_configured, 'OS ISO', saved.iso_configured ? 'Installation media saved. Preflight checks it again before deployment.' : 'Open the OS installation media tab to select or upload your ISO.', 'disc'), item(sshCount > 0, 'SSH access · optional', sshCount ? `${sshCount} public ${sshCount === 1 ? 'key will' : 'keys will'} be installed for gdeploy on newly queued deployments.` : 'Add your public keys for convenient access to future VMs.', 'key'), item(saved.splunk_configured, 'Splunk package · when selected', saved.splunk_configured ? 'Splunk package configured. Its license must be accepted for each deployment.' : 'Open Software packages to upload or select the Splunk installer and verify its checksum.', 'layers'), item(saved.fleetmanager_configured, 'FleetManager · when selected', saved.fleetmanager_configured ? `${saved.fleetmanager_mode === 'offline' ? 'Offline package' : 'Online repository'}, community string, and license configured.` : 'Open Software packages → FleetManager to configure installation, community string, and license.', 'server')));
    }
    mediaControls = createMediaPanel(catalog => { if (state.settings) state.settings.iso_configured = catalog.ready; renderReadiness(); }, value => { mediaBusy = value; syncControls(); });
    sshControls = createSSHAccessPanel(count => { if (state.settings) state.settings.ssh_key_count = count; renderReadiness(); }, value => { sshBusy = value; syncControls(); }, initialSSHdraft);
    packagesControls = createSplunkPackagePanel(catalog => { if (state.settings) state.settings.splunk_configured = catalog.ready; invalidatePreflight(); renderReadiness(); }, value => { packagesBusy = value; syncControls(); });
    fleetControls = createFleetManagerPanel(catalog => { if (state.settings) { state.settings.fleetmanager_configured = catalog.ready; state.settings.fleetmanager_mode = catalog.mode; } invalidatePreflight(); renderReadiness(); }, value => { fleetBusy = value; syncControls(); }, {onDirty: invalidatePreflight});
    panels.connection = el('div', {id: 'setup-panel-connection', role: 'tabpanel', 'aria-labelledby': 'setup-tab-connection', tabindex: '0'}, form, defaultsPanel);
    panels.media = el('div', {id: 'setup-panel-media', role: 'tabpanel', 'aria-labelledby': 'setup-tab-media', tabindex: '0'}, mediaControls.panel);
    panels.ssh = el('div', {id: 'setup-panel-ssh', role: 'tabpanel', 'aria-labelledby': 'setup-tab-ssh', tabindex: '0'}, sshControls.panel);
    const softwareTabs = el('div', {class: 'software-tabs', role: 'tablist', 'aria-label': 'Software configuration'});
    const softwareButtons = {}, softwarePanels = {};
    function selectSoftware(key, focus = false, updateHash = true) {
      state.packageTab = key;
      for (const [name, control] of Object.entries(softwareButtons)) {
        const selected = name === key; control.setAttribute('aria-selected', String(selected)); control.tabIndex = selected ? 0 : -1;
        softwarePanels[name].hidden = !selected;
      }
      if (updateHash) history.replaceState(history.state, '', `#settings/packages${key === 'splunk' ? '' : `/${key}`}`);
      if (focus) softwareButtons[key].focus();
      fleetControls.activate();
    }
    for (const [key, label, content] of [['splunk', 'Splunk Enterprise', packagesControls.panel], ['fleetmanager', 'FleetManager', fleetControls.panel]]) {
      softwareButtons[key] = el('button', {type: 'button', id: `software-tab-${key}`, role: 'tab', 'aria-controls': `software-panel-${key}`, onClick: () => selectSoftware(key), onKeydown: event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); selectSoftware(event.key === 'Home' ? 'splunk' : event.key === 'End' ? 'fleetmanager' : key === 'splunk' ? 'fleetmanager' : 'splunk', true);
      }}, label);
      softwarePanels[key] = el('div', {id: `software-panel-${key}`, role: 'tabpanel', 'aria-labelledby': `software-tab-${key}`}, content);
      softwareTabs.append(softwareButtons[key]);
    }
    panels.packages = el('div', {id: 'setup-panel-packages', role: 'tabpanel', 'aria-labelledby': 'setup-tab-packages', tabindex: '0'}, softwareTabs, softwarePanels.splunk, softwarePanels.fleetmanager);
    selectSoftware(state.packageTab || 'splunk', false, false);
    const draftBanner = returnToWizard ? el('div', {class: 'connection-banner'}, icon('clock'), el('div', {}, el('strong', {}, 'Your deployment draft is saved in this tab'), el('p', {}, 'Finish setup, then return to your VM names, resources, and network settings. Preflight will run again before deployment.')), returnToWizard) : null;
    page.replaceChildren(...[heading('Setup', 'Configure your host, OS installation media, SSH access, and software packages.'), draftBanner, steps, el('div', {class: 'settings-grid'}, el('div', {class: 'settings-main'}, panels.connection, panels.media, panels.ssh, panels.packages), el('aside', {}, readiness, el('p', {class: 'settings-note'}, 'Elasticsearch and Kibana are installed from Elastic’s package repository; guests need outbound network access.')))].filter(Boolean));
    selectSetupTab(state.setupTab || (settings.configured && !settings.iso_configured ? 'media' : 'connection'), {updateHash: false});
    renderReadiness(); renderCertificate(); renderNetworkDefaults(); syncControls(); mediaControls.load(); packagesControls.load(); fleetControls.load(); sshControls.load();
  }
  function field(label, input, hint) { return el('label', {class: 'field'}, el('span', {}, label), input, hint ? el('small', {}, hint) : null); }
  function renderDeploymentControls(data) {
    const controls = el('div', {id: 'deployment-controls', class: 'deployment-controls'});
    if (stoppableStatuses.has(data.status)) controls.append(el('section', {class: 'surface'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Deployment controls')), el('div', {class: 'recovery-body'}, el('p', {}, 'Stop GDeploy’s automation while keeping existing VMs, disks, and data.'), stopButton(data))));
    if (data.status === 'stopping') controls.append(el('section', {class: 'surface', id: 'deployment-stop-status', role: 'status'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Stop requested')), el('div', {class: 'recovery-body'}, el('p', {}, 'GDeploy is waiting for its current operation to finish. Existing VMs and disks are preserved; VMs are not powered off.'), stopButton(data))));
    if (data.status === 'stopped') controls.append(el('section', {class: 'surface', id: 'deployment-stop-status', role: 'status'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Deployment stopped')), el('div', {class: 'recovery-body'}, el('p', {}, 'GDeploy has stopped its automation. Existing VMs and disks are preserved; work already started inside a VM may continue. This deployment cannot be resumed.'), el('p', {}, 'Keep the VMs and hide this record, or use the separate Delete & redeploy action below to start over.'))));
    if (data.hidden_at || hideableStatuses.has(data.status)) controls.append(el('section', {class: 'surface history-card'}, el('div', {class: 'surface-header'}, el('h2', {}, 'History visibility')), el('div', {class: 'recovery-body'}, el('p', {}, data.hidden_at ? 'This record is hidden from the default history view. Its deployment status is unchanged.' : 'Keep the VMs and hide this record from the default history view. Find it again with Show hidden. Credentials and logs stay available.'), visibilityButton(data))));
    if (failureStatuses.has(data.status) || data.status === 'stopped') controls.append(el('section', {class: 'surface recovery-card'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Start over')), el('div', {class: 'recovery-body'}, el('p', {}, 'If the VM is working, you can keep it and hide this record instead. Starting over permanently deletes the VMs, all data on their disks, and installation media owned by this deployment.'), el('p', {}, 'A replacement uses the same VM configuration and the current OS ISO selection in Setup.'), button(data.status === 'cleanup_failed' ? 'Retry cleanup & redeploy' : 'Delete & redeploy', 'button-danger button-full', () => openRedeploy(data), 'refresh'))));
    return controls;
  }
  function renderDetail(data) {
    state.detailPending = null;
    const previousLogBody = $('#deployment-log-body');
    const logScrollTop = previousLogBody?.scrollTop || 0;
    const logsAtEnd = previousLogBody && previousLogBody.scrollHeight - previousLogBody.clientHeight - logScrollTop < 12;
    const focusedLogControl = $('#deployment-logs')?.contains(document.activeElement) ? document.activeElement.id : null;
    const focusedHistoryControl = document.activeElement?.id === `history-visibility-${data.id}` ? document.activeElement.id : null;
    const vms = data.vms || [];
    const back = el('a', {class: 'back-link', href: '#deployments'}, icon('back'), 'All deployments');
    const title = el('div', {class: 'page-heading'}, el('div', {}, el('div', {class: 'detail-title'}, el('h1', {}, data.name), el('span', {id: 'deployment-detail-status', role: 'status'}, statusBadge(data.status))), el('p', {class: 'detail-meta'}, `${vms.length} ${vms.length === 1 ? 'virtual machine' : 'virtual machines'} · Created ${date(data.created_at, true)}`)), button('Refresh', 'button-ghost button-small', () => refreshDetail(true), 'refresh'));
    const vmSurface = el('section', {class: 'surface'}, el('div', {class: 'surface-header'}, el('h2', {}, 'Virtual machines', el('span', {class: 'count-badge'}, vms.length))));
    for (const vm of vms) {
      const spec = (label, value) => el('div', {}, el('dt', {}, label), el('dd', {}, value || '—'));
      vmSurface.append(el('article', {class: 'vm-card'}, el('div', {class: 'vm-card-head'}, el('div', {class: 'vm-card-name'}, roleIcon(vm.role), el('div', {}, el('h3', {}, vm.name), el('p', {}, roleName(vm.role)))), vm.status ? statusBadge(vm.status) : null), el('dl', {class: 'vm-specs'}, spec('CPU', `${vm.cpu} vCPU`), spec('Memory', `${vm.ram_gb} GB`), spec('Storage', `${vm.disk_gb} GB`), spec('Datastore', vm.datastore), spec('Network', vm.network), spec('IP address', vm.ip || (vm.ip_mode === 'static' ? vm.address : 'DHCP · pending'))), vm.services?.length ? el('div', {class: 'service-links'}, vm.services.map(serviceLink)) : null));
    }
    if (!vms.length) vmSurface.append(el('p', {class: 'no-events'}, 'VM details will appear as provisioning starts.'));
    const logList = el('ol', {class: 'log-list', 'aria-label': 'Deployment events'});
    for (const event of data.events || []) {
      const severity = ['error', 'warning', 'info', 'debug'].includes(event.level) ? event.level : 'info';
      logList.append(el('li', {class: `log-item ${severity}`}, el('div', {class: 'log-meta'}, el('time', {datetime: event.at || '', title: date(event.at, true)}, eventTime(event.at)), el('span', {class: 'log-level'}, severity.toUpperCase())), el('span', {class: 'log-message'}, event.message)));
    }
    const logBody = el('div', {class: 'deployment-log-body', id: 'deployment-log-body', tabindex: '0', 'aria-label': 'Recorded deployment logs'}, data.events?.length ? logList : el('p', {class: 'no-events'}, 'No events recorded yet.'));
    const manualText = el('textarea', {id: 'deployment-log-manual-text', rows: 14, readOnly: true, spellcheck: 'false', 'aria-describedby': 'deployment-log-manual-help'});
    const manualDone = button('Done copying', 'button-small', () => { manualCopy.hidden = true; manualText.value = ''; logCopy.focus({preventScroll: true}); refreshDetail(); });
    manualDone.id = 'deployment-log-manual-done';
    const manualCopy = el('div', {id: 'deployment-log-manual-copy', class: 'log-manual-copy', hidden: true}, el('p', {id: 'deployment-log-manual-help', role: 'status'}, 'Automatic copying is blocked by your browser. Copy the selected text with Ctrl+C (⌘C on Mac), or use your browser’s Copy command. This log snapshot stays in place until you finish.'), field('Full deployment log', manualText), manualDone);
    const logCopy = button('Copy logs', 'button-small', async () => {
      const text = [`Deployment: ${data.name}`, `Status: ${statusNames[data.status] || data.status}`, ...(data.error ? [`Error: ${data.error}`] : []), '', ...(data.events || []).map(event => `${event.at || 'Time unavailable'} [${String(event.level || 'info').toUpperCase()}] ${event.message}`)].join('\n');
      const session = state.session, epoch = state.routeEpoch;
      logCopy.disabled = true; logCopy.dataset.copying = 'true';
      try {
        const copied = await copyText(text);
        if (!logCopy.isConnected || state.session !== session || state.routeEpoch !== epoch) return;
        if (copied) notify('Deployment logs copied.');
        else {
          if (!state.openLogs.has(data.id)) { notify('Automatic copying is blocked. Open the deployment logs and try again for manual copying.'); return; }
          manualText.value = text; manualCopy.hidden = false;
          manualText.focus({preventScroll: true}); manualText.select(); manualCopy.scrollIntoView({block: 'nearest'});
          notify('Automatic copying is blocked. Use the selected log text below.');
        }
      } finally { delete logCopy.dataset.copying; if (logCopy.isConnected) logCopy.disabled = false; }
    }, 'copy');
    logCopy.id = 'deployment-log-copy'; logCopy.disabled = !data.events?.length;
    const logContents = el('div', {id: 'deployment-log-contents'}, el('div', {class: 'log-toolbar'}, el('p', {}, 'Recorded events and installation-media diagnostics. Earlier releases may only have saved a summary; additional detail appears on a new attempt.'), logCopy), manualCopy, logBody);
    function setLogsOpen(open, focus = false) {
      if (open) state.openLogs.add(data.id); else state.openLogs.delete(data.id);
      if (!open) { manualCopy.hidden = true; manualText.value = ''; }
      logContents.hidden = !open;
      logToggle.setAttribute('aria-expanded', String(open));
      logToggle.replaceChildren(icon('terminal'), open ? 'Hide logs' : 'View logs');
      if (focus) { if (open) logBody.scrollTop = logBody.scrollHeight; logToggle.focus({preventScroll: true}); logs.scrollIntoView({block: 'nearest'}); }
    }
    const logToggle = button('View logs', 'button-small', () => setLogsOpen(!state.openLogs.has(data.id)), 'terminal');
    logToggle.id = 'deployment-log-toggle'; logToggle.setAttribute('aria-controls', 'deployment-log-contents');
    const logs = el('section', {class: 'surface', id: 'deployment-logs', 'aria-labelledby': 'deployment-logs-title'}, el('div', {class: 'surface-header'}, el('div', {}, el('h2', {id: 'deployment-logs-title'}, 'Deployment logs'), el('p', {}, `${data.events?.length || 0} recorded events${busyStatuses.has(data.status) ? ' · Updates automatically' : ''}`)), logToggle), logContents);
    setLogsOpen(state.openLogs.has(data.id));
    const failure = data.error ? el('div', {class: 'alert alert-error deployment-error', role: 'alert'}, el('strong', {}, 'Deployment needs attention'), el('p', {}, data.error), button('View deployment logs', 'button-small', () => setLogsOpen(true, true), 'terminal')) : null;
    const hiddenNotice = data.hidden_at ? el('div', {class: 'visibility-notice', role: 'status'}, icon('info'), el('div', {}, el('strong', {}, 'Hidden from deployment history'), el('p', {}, 'The recorded status, VMs, credentials, and logs are preserved. Restore this record using History visibility.'))) : null;
    const left = el('div', {class: 'detail-main'}, hiddenNotice, failure, vmSurface, logs);
    const credentials = renderCredentials();
    const aside = el('aside', {class: 'detail-aside'}, renderProgress(data), credentials, renderDeploymentControls(data));
    page.replaceChildren(back, title, el('div', {class: 'detail-layout'}, left, aside));
    if (state.openLogs.has(data.id)) logBody.scrollTop = logsAtEnd ? logBody.scrollHeight : logScrollTop;
    if (focusedLogControl) document.getElementById(focusedLogControl)?.focus({preventScroll: true});
    if (focusedHistoryControl) document.getElementById(focusedHistoryControl)?.focus({preventScroll: true});
  }
  function renderProgress(data) {
    const index = stageOrder.indexOf(data.stage);
    const completed = data.status === 'completed';
    const failed = failureStatuses.has(data.status);
    const stopping = data.status === 'stopping', stopped = data.status === 'stopped';
    const percentage = completed ? 100 : index > 0 ? Math.round(index / (stageOrder.length - 1) * 100) : 0;
    const list = el('ol', {class: 'stage-list'});
    const stages = [['preflight', 'Preflight checks'], ['preparing', 'Prepare installation media'], ['creating', 'Create virtual machines'], ['installing_os', 'Install operating system'], ['installing_software', 'Install selected software'], ['verifying', 'Verify services']];
    for (const [key, label] of stages) {
      const stageIndex = stageOrder.indexOf(key);
      const done = completed || (index >= 0 && stageIndex < index);
      const current = stageIndex === index;
      list.append(el('li', {class: `${done ? 'done' : current ? 'current' : ''} ${failed ? 'failed' : ''}`}, el('span', {class: 'stage-marker'}, done ? icon('check') : current ? '•' : String(stageIndex)), label, current ? el('span', {class: 'stage-note'}, stopped ? 'Last stage' : 'Current') : null));
    }
    return el('section', {id: 'deployment-progress', class: 'surface progress-card'}, el('div', {class: 'card-label'}, 'Deployment progress'), el('div', {class: 'progress-top'}, el('strong', {}, stopping || stopped ? statusNames[data.status] : stageNames[data.stage] || statusNames[data.status] || 'Preparing deployment'), el('span', {}, completed ? 'Complete' : stopped ? 'Automation stopped' : stopping ? 'Stop requested' : failed ? 'Stopped' : '')), el('div', {class: 'progress-track', role: 'progressbar', 'aria-label': 'Completed deployment stages', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': percentage}, el('div', {class: `progress-fill progress-${percentage} ${failed ? 'failed' : ''}`})), list);
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
          if (service.password) section.append(secretField(service.password_change_required ? 'Temporary admin password' : 'Service password', service.password));
          if (service.password_change_required) section.append(el('p', {class: 'credentials-first-login'}, 'Change this temporary administrator password when you first sign in to FleetManager. GDeploy’s saved value will remain the original temporary password.'));
          if (service.community_string) section.append(secretField('Community string', service.community_string));
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
  function applyDetailUpdate(data) {
    const selection = window.getSelection();
    const selectingLogs = state.openLogs.has(data.id) && selection && !selection.isCollapsed && $('#deployment-logs')?.contains(selection.anchorNode);
    const copyingLogs = $('#deployment-log-copy')?.dataset.copying === 'true' || $('#deployment-log-manual-copy')?.hidden === false;
    if (selectingLogs || copyingLogs) {
      // Keep the user's log snapshot and selection intact while lifecycle state updates.
      const previous = state.detailPending || state.detail;
      state.detailPending = data;
      if (!previous || previous.status !== data.status || previous.stage !== data.stage || previous.hidden_at !== data.hidden_at) {
        $('#deployment-detail-status')?.replaceChildren(statusBadge(data.status));
        $('#deployment-progress')?.replaceWith(renderProgress(data));
        $('#deployment-controls')?.replaceWith(renderDeploymentControls(data));
      }
    } else { state.detail = data; state.detailPending = null; renderDetail(data); }
  }
  async function refreshDetail(manual = false) {
    if (!state.session || setupRequired()) return;
    const id = state.detailId; const epoch = state.routeEpoch; const revision = state.historyRevision;
    if (!id || state.visibilityBusy.has(id) || state.stopBusy.has(id)) return;
    const request = ++state.detailRequest;
    try {
      const data = await api(`/api/deployments/${encodeURIComponent(id)}`);
      if (id !== state.detailId || epoch !== state.routeEpoch || request !== state.detailRequest || revision !== state.historyRevision || state.visibilityBusy.has(id) || state.stopBusy.has(id)) return;
      if (JSON.stringify(data) !== JSON.stringify(state.detail)) applyDetailUpdate(data);
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
    if (state.wizard?.busy) return;
    if (!state.settings?.configured || !state.settings?.iso_configured) { location.hash = state.settings?.configured ? 'settings/media' : 'settings/connection'; notify('Complete Setup with an ESXi connection and an OS ISO to begin a deployment.'); return; }
    hideCredentials();
    const dialog = $('#wizard-dialog');
    if (state.wizard?.suspended) {
      state.wizard.suspended = false; invalidatePreflight(); state.wizard.accepted = false; state.wizard.busy = true;
      state.wizard.fleetControls = null; state.wizard.fleetValidated = false;
      if (state.wizard.selected.has('fleetmanager') && state.wizard.step > 2) state.wizard.step = 2;
    }
    else state.wizard = {step: 0, name: '', selected: new Set(), vms: {}, accepted: false, autoDeploy: false, preflight: null, busy: true, error: ''};
    dialog.replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), loading('Loading ESXi datastores and networks…')));
    dialog.showModal();
    const wizard = state.wizard;
    try {
      const results = await Promise.allSettled([api('/api/inventory'), api('/api/settings')]);
      if (state.wizard !== wizard) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      if (results[1].status === 'rejected') throw results[1].reason;
      state.inventory = results[0].value; state.settings = results[1].value; state.inventoryHost = state.settings.host;
      wizard.busy = false;
      renderWizard();
    } catch (error) {
      if (state.wizard !== wizard) return;
      wizard.busy = false;
      dialog.replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), el('div', {class: 'wizard-content'}, el('h3', {}, 'Couldn’t load your deployment settings'), el('p', {class: 'muted'}, 'Check the saved connection and your host’s certificate, permissions, and availability.'), el('div', {class: 'alert alert-error', role: 'alert'}, error.message), wizardSetupLink('Open Setup', '#settings/connection'))));
    }
  }
  function closeWizard() {
    if (state.wizard?.busy) return;
    $('#wizard-dialog').close(); $('#wizard-dialog').replaceChildren(); state.wizard = null;
    if (state.route === 'deployments') renderOverview();
  }
  function wizardSetupLink(label, href = '#settings/packages') {
    if (!/^#settings\/(connection|media|ssh|packages(?:\/(?:splunk|fleetmanager))?)$/.test(href)) return null;
    return el('a', {href, class: 'button button-small wizard-setup-link', onClick: event => {
      event.preventDefault();
      if (!state.wizard || state.wizard.busy) return;
      invalidatePreflight(); state.wizard.suspended = true;
      $('#wizard-dialog').close(); location.hash = href;
    }}, label, icon('arrow'));
  }
  $('#wizard-dialog').addEventListener('cancel', event => { if (state.wizard?.busy) event.preventDefault(); else { $('#wizard-dialog').replaceChildren(); state.wizard = null; if (state.route === 'deployments') renderOverview(); } });
  function wizardHeader() {
    const close = el('button', {type: 'button', class: 'icon-button dialog-close', 'aria-label': 'Close deployment wizard', onClick: closeWizard}, icon('close'));
    return el('div', {class: 'wizard-header'}, el('div', {}, el('h2', {id: 'wizard-title'}, 'New deployment'), el('p', {}, 'A ready-to-use environment, configured your way.')), close);
  }
  function selectedRoles() { return Object.keys(roles).filter(role => state.wizard.selected.has(role)); }
  function wizardSteps() {
    return ['Choose software', 'Configure VMs', ...(state.wizard.selected.has('fleetmanager') ? ['FleetManager configuration'] : []), 'Review & deploy'];
  }
  function isFleetStep() { return state.wizard?.selected.has('fleetmanager') && state.wizard.step === 2; }
  function syncFleetWizardActions(wizard) {
    if (state.wizard !== wizard || !isFleetStep()) return;
    for (const control of $('#wizard-dialog').querySelectorAll('.wizard-footer button,.dialog-close')) control.disabled = wizard.busy;
    const next = $('#wizard-next');
    if (next) {
      next.disabled = wizard.busy || !wizard.fleetControls?.canSave();
      next.replaceChildren(...(wizard.busy ? [spinner(), 'Please wait…'] : [icon('arrow'), 'Save & continue']));
    }
  }
  function renderFleetConfiguration(content) {
    const wizard = state.wizard;
    content.append(el('h3', {}, 'FleetManager configuration'), el('p', {class: 'muted'}, 'Choose how to install FleetManager, select an online version from the repository, and provide its community string, license, and repository token or offline packages. Saved values can be reused. Complete this step before running preflight.'));
    if (!wizard.fleetControls) {
      wizard.fleetControls = createFleetManagerPanel(catalog => {
        if (state.wizard !== wizard) return;
        if (state.settings) { state.settings.fleetmanager_configured = catalog.ready; state.settings.fleetmanager_mode = catalog.mode; }
        wizard.fleetCatalog = catalog;
        wizard.fleetValidated = false; invalidatePreflight();
      }, busy => {
        if (state.wizard !== wizard) return;
        wizard.busy = busy; syncFleetWizardActions(wizard);
      }, {
        wizard: true, isCurrent: () => state.wizard === wizard && !wizard.suspended && isFleetStep(),
        onDirty: () => { wizard.fleetValidated = false; invalidatePreflight(); }, onContinue: wizardNext,
      });
    }
    content.append(wizard.fleetControls.panel);
  }
  function defaultVMName(name, role) { return `${(name || 'environment').slice(0, 62 - role.length).replace(/-+$/, '')}-${role}`; }
  function savedNetworkDefault() {
    const saved = state.settings?.deployment_defaults;
    const name = saved?.default_network || '';
    const sameHost = Boolean(state.settings?.host) && saved?.applies_to_host === true;
    const available = sameHost && endpointIdentity(state.inventoryHost) === endpointIdentity(state.settings.host) && (state.inventory?.networks || []).filter(network => network.name === name).length === 1;
    return {name, sameHost, available, host: saved?.host};
  }
  function ensureVMs() {
    const wizard = state.wizard;
    const networkDefault = savedNetworkDefault();
    for (const role of selectedRoles()) {
      if (!wizard.vms[role]) wizard.vms[role] = {role, name: defaultVMName(wizard.name, role), cpu: roles[role].cpu, ram_gb: roles[role].ram, disk_gb: roles[role].disk, datastore: state.inventory?.datastores?.[0]?.name || '', network: networkDefault.available ? networkDefault.name : '', ip_mode: 'dhcp', address: '', gateway: '', dnsText: ''};
    }
  }
  function invalidatePreflight() {
    if (!state.wizard) return;
    state.wizard.preflight = null; state.wizard.error = ''; state.wizard.vmError = null;
    for (const alert of $('#wizard-dialog').querySelectorAll('.vm-validation-error')) alert.remove();
    for (const input of $('#wizard-dialog').querySelectorAll('[aria-invalid]')) { input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby'); }
  }
  function renderWizard() {
    const wizard = state.wizard; if (!wizard) return;
    const focusedId = $('#wizard-dialog').contains(document.activeElement) ? document.activeElement.id : '';
    ensureVMs();
    const steps = wizardSteps(), reviewStep = steps.length - 1;
    const loadFleet = isFleetStep() && !wizard.fleetControls;
    const sidebar = el('aside', {class: 'wizard-sidebar', 'aria-label': 'Deployment steps'});
    steps.forEach((label, index) => sidebar.append(el('div', {class: `wizard-step ${index === wizard.step ? 'active' : index < wizard.step ? 'done' : ''}`, ...(index === wizard.step ? {'aria-current': 'step'} : {})}, el('b', {}, index < wizard.step ? '✓' : index + 1), el('span', {}, label))));
    sidebar.append(el('p', {class: 'wizard-aside-note'}, 'The OS ISO selected in Setup is used for each VM. Every selected role gets its own dedicated machine.'));
    const content = el('div', {class: 'wizard-content', id: 'wizard-content'});
    if (wizard.error && !wizard.vmError) content.append(el('div', {class: 'alert alert-error', role: 'alert'}, wizard.error));
    if (wizard.step === 0) renderBlueprint(content);
    if (wizard.step === 1) renderResources(content);
    if (isFleetStep()) renderFleetConfiguration(content);
    if (wizard.step === reviewStep) renderReview(content);
    const footer = el('div', {class: 'wizard-footer'}, el('span', {}, `Step ${wizard.step + 1} of ${steps.length}`));
    const actions = el('div', {class: 'wizard-footer-actions'});
    if (wizard.step > 0) actions.append(button('Back', 'button-ghost', () => { if (wizard.busy) return; wizard.step--; renderWizard(); }));
    else actions.append(button('Cancel', 'button-ghost', closeWizard));
    const nextLabel = isFleetStep() ? 'Save & continue' : wizard.step < reviewStep ? 'Continue' : wizard.preflight?.ok ? `Deploy ${selectedRoles().length} ${selectedRoles().length === 1 ? 'VM' : 'VMs'}` : wizard.autoDeploy ? 'Run preflight & deploy' : 'Run preflight';
    const next = button(nextLabel, 'button-primary', wizardNext, wizard.step < reviewStep ? 'arrow' : wizard.preflight?.ok ? 'plus' : 'shield'); next.id = 'wizard-next';
    next.disabled = wizard.busy || (wizard.step === 0 && !wizard.selected.size);
    if (wizard.busy) setBusy(next, wizard.preflight?.ok ? 'Queuing deployment…' : 'Checking prerequisites…');
    for (const action of actions.children) action.disabled = wizard.busy;
    actions.append(next); footer.append(actions);
    $('#wizard-dialog').replaceChildren(el('div', {class: 'wizard-frame'}, wizardHeader(), el('div', {class: 'wizard-layout'}, sidebar, content), footer));
    if (wizard.busy) for (const control of $('#wizard-dialog').querySelectorAll('button,input,select')) control.disabled = true;
    else if (focusedId) document.getElementById(focusedId)?.focus({preventScroll: true});
    if (isFleetStep()) { syncFleetWizardActions(wizard); if (loadFleet) wizard.fleetControls.load(); }
  }
  function renderBlueprint(content) {
    const wizard = state.wizard;
    content.append(el('h3', {}, 'What are we deploying?'), el('p', {class: 'muted'}, 'Name your environment, then choose the software to provision. You can adjust every VM in the next step.'));
    const name = el('input', {id: 'deployment-name', type: 'text', value: wizard.name, required: true, maxlength: 63, pattern: '[a-z](?:(?:[a-z0-9]|-)*[a-z0-9])?', placeholder: 'e.g. observability-lab', autocomplete: 'off', spellcheck: 'false', onInput: event => {
      const previous = wizard.name;
      if (wizard.busy) return;
      wizard.name = event.target.value; invalidatePreflight();
      for (const role of Object.keys(wizard.vms)) if (wizard.vms[role].name === defaultVMName(previous, role)) wizard.vms[role].name = defaultVMName(wizard.name, role);
    }});
    content.append(field('Deployment name', name, 'Use lowercase letters, numbers, and hyphens. VM names use this as a starting point.'));
    const grid = el('div', {class: 'role-grid'});
    for (const [role, config] of Object.entries(roles)) {
      const checkbox = el('input', {id: `role-${role}`, type: 'checkbox', checked: wizard.selected.has(role), 'aria-label': config.name, onChange: event => {
        if (wizard.busy) return;
        if (event.target.checked) wizard.selected.add(role); else wizard.selected.delete(role);
        if (role === 'kibana' && event.target.checked) wizard.selected.add('elasticsearch');
        if (role === 'elasticsearch' && !event.target.checked) wizard.selected.delete('kibana');
        invalidatePreflight(); renderWizard();
      }});
      const readiness = role === 'splunk' ? state.settings?.splunk_configured : role === 'fleetmanager' ? state.settings?.fleetmanager_configured : null;
      const packageStatus = ['splunk', 'fleetmanager'].includes(role) ? el('small', {class: `role-package-status ${readiness ? 'ready' : ''}`}, readiness ? role === 'fleetmanager' ? `${state.settings.fleetmanager_mode === 'offline' ? 'Offline package' : 'Online repository'} configured` : 'Installer package configured' : role === 'fleetmanager' ? 'Configure after VM resources' : 'Installer package needed in Setup') : null;
      grid.append(el('label', {class: 'role-option'}, checkbox, roleIcon(role), el('span', {class: 'role-option-text'}, el('strong', {}, config.name), el('small', {}, config.description), packageStatus)));
    }
    content.append(el('div', {class: 'field-section-label'}, 'Software & roles', el('span', {}, `${wizard.selected.size} ${wizard.selected.size === 1 ? 'VM' : 'VMs'} selected`)), grid, el('div', {class: 'wizard-callout'}, icon('info'), el('span', {}, 'Selecting Kibana also selects Elasticsearch. GDeploy configures their connection, matching versions, and TLS. Splunk and FleetManager each run on their own VM.')));
    if (wizard.selected.has('splunk') && !state.settings?.splunk_configured) content.append(el('div', {class: 'wizard-package-notice'}, el('p', {}, 'Add the Splunk Enterprise Linux x86_64 .tgz package and its publisher checksum in Setup. Your deployment draft will be kept while you do this.'), wizardSetupLink('Configure Splunk package')));
    if (wizard.selected.has('fleetmanager')) content.append(el('div', {class: 'wizard-package-notice'}, el('p', {}, 'After configuring VM resources, you’ll choose FleetManager’s installation method and provide its community string, license, and installation credentials before preflight.')));
  }
  function renderResources(content) {
    const wizard = state.wizard;
    const selected = selectedRoles();
    content.append(el('h3', {}, `Configure ${selected.length} ${selected.length === 1 ? 'virtual machine' : 'separate virtual machines'}`), el('p', {class: 'muted'}, 'Every section below creates a dedicated VM. Give each machine its own name, resources, storage destination, and network settings.'));
    const networkDefault = savedNetworkDefault();
    if (networkDefault.name && !networkDefault.available) content.append(el('div', {id: 'wizard-network-default-warning', class: 'alert alert-warning', role: 'status'}, networkDefault.sameHost ? `The saved default port group “${networkDefault.name}” is unavailable or ambiguous. Choose an available network / port group with a unique name for each VM.` : `The saved default port group belongs to ESXi host ${networkDefault.host || 'unavailable'}. Choose a network / port group on the current host for each VM.`));
    if (selected.length > 1) content.append(el('nav', {class: 'vm-jump-links', 'aria-label': 'VM configuration sections'}, selected.map(role => button(`${roles[role].short} VM`, 'button-small', () => {
      document.getElementById(`vm-${role}-name`)?.focus({preventScroll: true});
      document.getElementById(`vm-resource-${role}`)?.scrollIntoView({block: 'start'});
    }, roles[role].icon))));
    for (const [index, role] of selected.entries()) {
      const vm = wizard.vms[role];
      const limits = roleLimits(role);
      const form = el('section', {class: 'vm-resource-form', id: `vm-resource-${role}`, 'data-vm-role': role, 'aria-labelledby': `vm-heading-${role}`});
      const invalidAttrs = key => wizard.vmError?.role === role && wizard.vmError.key === key ? {'aria-invalid': 'true', 'aria-describedby': `vm-error-${role}`} : {};
      const input = (key, attrs = {}) => el('input', {id: `vm-${role}-${key}`, ...attrs, ...invalidAttrs(key), value: vm[key], onInput: event => { vm[key] = ['cpu', 'ram_gb', 'disk_gb'].includes(key) ? Number(event.target.value) : event.target.value; invalidatePreflight(); }});
      form.append(...[el('div', {class: 'vm-form-header'}, roleIcon(role), el('div', {}, el('h4', {id: `vm-heading-${role}`}, `${roleName(role)} VM`), el('p', {}, 'Operating system from your selected OS ISO')), el('span', {class: 'vm-form-number'}, `VM ${index + 1} of ${selected.length}`)), wizard.vmError?.role === role ? el('div', {id: `vm-error-${role}`, class: 'alert alert-error vm-validation-error', role: 'alert', tabindex: '-1'}, wizard.vmError.error) : null, field('Virtual machine name', input('name', {type: 'text', required: true, maxlength: 63, pattern: '[a-z](?:(?:[a-z0-9]|-)*[a-z0-9])?', autocomplete: 'off', spellcheck: 'false'}), 'A unique hostname starting with a letter; lowercase letters, numbers, and hyphens only.'), el('div', {class: 'field-grid three'}, field('CPU cores', input('cpu', {type: 'number', min: limits.cpu, max: 128, step: 1, required: true}), 'vCPU'), field('Memory', input('ram_gb', {type: 'number', min: limits.ram, max: 2048, step: 1, required: true}), 'GB RAM'), field('Disk size', input('disk_gb', {type: 'number', min: limits.disk, max: 65536, step: 1, required: true}), 'GB · thin provisioned'))].filter(Boolean));
      if (role === 'fleetmanager') form.append(el('p', {class: 'media-help'}, 'FleetManager requires at least 2 CPUs, 8 GB RAM, and enough disk for 30 GB in /var, 20 GB in /tmp, and the operating system. The 80 GB default provides additional headroom.'));
      const datastore = el('select', {id: `vm-${role}-datastore`, ...invalidAttrs('datastore'), required: true, onChange: event => { vm.datastore = event.target.value; invalidatePreflight(); }}, el('option', {value: ''}, 'Choose a datastore'), (state.inventory?.datastores || []).map(store => el('option', {value: store.name}, `${store.name} · ${Math.floor(Number(store.free_gb))} GB free`))); datastore.value = vm.datastore;
      const networks = state.inventory?.networks || [], networkCount = name => networks.filter(item => item.name === name).length;
      const network = el('select', {id: `vm-${role}-network`, ...invalidAttrs('network'), required: true, onChange: event => { vm.network = event.target.value; invalidatePreflight(); }}, el('option', {value: ''}, 'Choose a port group'), [...new Set(networks.map(item => item.name))].map(name => el('option', {value: name, disabled: networkCount(name) !== 1}, networkCount(name) === 1 ? name : `${name} · ambiguous name`))); network.value = vm.network;
      if (vm.network && networkCount(vm.network) !== 1) { if (!networkCount(vm.network)) network.append(el('option', {value: vm.network, disabled: true}, `${vm.network} · unavailable on this host`)); network.value = vm.network; form.append(el('p', {class: 'alert alert-warning'}, 'This VM’s selected port group is unavailable or ambiguous on the current ESXi host. Choose a group with a unique name before continuing.')); }
      form.append(el('div', {class: 'field-grid'}, field('Datastore', datastore, 'Storage destination on the ESXi host.'), field('Network / port group', network, 'Must be reachable from GDeploy.')), el('hr', {class: 'form-divider'}), el('div', {class: 'field-section-label'}, 'IP address configuration'));
      const staticFields = el('div', {class: 'static-fields', hidden: vm.ip_mode !== 'static'}, el('div', {class: 'field-grid'}, field('IPv4 address / prefix', input('address', {type: 'text', required: true, placeholder: '192.168.1.20/24', autocomplete: 'off', spellcheck: 'false'}), 'Include the subnet prefix, for example /24.'), field('Default gateway', input('gateway', {type: 'text', required: true, placeholder: '192.168.1.1', autocomplete: 'off', spellcheck: 'false'}))), field('DNS servers', input('dnsText', {type: 'text', required: true, placeholder: '192.168.1.1, 1.1.1.1', autocomplete: 'off', spellcheck: 'false'}), 'Separate multiple IPv4 addresses with commas.'));
      const dhcpNote = el('p', {class: 'network-note', hidden: vm.ip_mode !== 'dhcp'}, 'A DHCP server on this network must provide an address, gateway, and DNS. GDeploy discovers the guest address through VMware Tools.');
      const choices = el('div', {class: 'network-choices', role: 'radiogroup', 'aria-label': `${roleName(role)} IP address configuration`});
      for (const [mode, label] of [['dhcp', 'Automatic (DHCP)'], ['static', 'Static IP']]) choices.append(el('label', {}, el('input', {id: `vm-${role}-network-${mode}`, type: 'radio', name: `ip-mode-${role}`, value: mode, checked: vm.ip_mode === mode, onChange: () => { vm.ip_mode = mode; invalidatePreflight(); staticFields.hidden = mode !== 'static'; dhcpNote.hidden = mode !== 'dhcp'; }}), label));
      form.append(choices, staticFields, dhcpNote);
      content.append(form);
    }
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
    if (wizard.selected.has('fleetmanager')) {
      const fleet = wizard.fleetCatalog || {mode: state.settings?.fleetmanager_mode};
      const installation = fleet.mode === 'offline' ? 'Offline .deb packages. Package installation uses only the supplied files and installed dependencies.' : 'Online vendor repository. The VM needs internet access; the repository remains configured for future updates. Version availability is checked on the VM during installation.';
      content.append(el('div', {class: 'wizard-callout', id: 'fleetmanager-review'}, icon('server'), el('span', {}, `FleetManager installation: ${installation} ${fleetManagerVersionSummary(fleet)} Saved community string and license are applied during installation.`)));
    }
    if (wizard.selected.has('splunk')) content.append(el('div', {class: 'license-box'}, el('label', {class: 'check-label'}, el('input', {type: 'checkbox', checked: wizard.accepted, disabled: wizard.busy, onChange: event => { if (wizard.busy) return; wizard.accepted = event.target.checked; invalidatePreflight(); renderWizard(); }}), el('span', {}, 'I have reviewed and accept the Splunk license terms applicable to the supplied package, and I authorize unattended acceptance during installation.'))));
    content.append(el('div', {class: 'wizard-callout'}, icon('key'), el('span', {}, 'Linux and application credentials are generated during provisioning. Reveal them from the Credentials panel on the deployment page.')));
    content.append(el('div', {class: 'license-box'}, el('label', {class: 'check-label'}, el('input', {id: 'wizard-auto-deploy', type: 'checkbox', checked: wizard.autoDeploy, disabled: wizard.busy, onChange: event => { if (wizard.busy) return; wizard.autoDeploy = event.target.checked; invalidatePreflight(); renderWizard(); }}), el('span', {}, 'Start deployment automatically when preflight passes')), el('p', {class: 'media-help'}, 'When selected, Run preflight & deploy queues this deployment as soon as all checks pass. Failed checks stop the process.')));
    const preflight = el('div', {class: 'preflight-box'}, el('div', {class: 'preflight-title'}, el('h4', {}, 'Preflight checks'), el('span', {}, wizard.preflight ? wizard.preflight.ok ? 'All checks passed' : 'Resolve failed checks' : 'Not run yet')));
    if (wizard.preflight) {
      const checks = el('div', {}, (wizard.preflight.checks || []).map(check => {
        let action = null;
        if (!check.ok && check.action?.href && typeof check.action.label === 'string') {
          action = check.action.href === '#settings/packages/fleetmanager' && wizard.selected.has('fleetmanager')
            ? button('Edit FleetManager configuration', 'button-small wizard-setup-link', () => { if (wizard.busy) return; invalidatePreflight(); wizard.fleetControls = null; wizard.fleetValidated = false; wizard.step = 2; renderWizard(); }, 'back')
            : wizardSetupLink(check.action.label, check.action.href);
        }
        return el('div', {class: `preflight-check ${check.ok ? '' : 'fail'}`}, icon(check.ok ? 'checkCircle' : 'alert'), el('div', {}, el('strong', {}, check.name), el('p', {}, check.message), action));
      }));
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
      let invalid = null;
      const limits = roleLimits(vm.role);
      if (!validHostname(vm.name)) invalid = ['name', 'Use a VM name of 1–63 lowercase letters, numbers, or hyphens, beginning with a letter and ending with a letter or number.'];
      else if (names.has(vm.name)) invalid = ['name', 'Every virtual machine needs a unique name.'];
      else if (!Number.isInteger(vm.cpu) || vm.cpu < limits.cpu || vm.cpu > 128) invalid = ['cpu', `CPU cores must be a whole number from ${limits.cpu} to 128.`];
      else if (!Number.isInteger(vm.ram_gb) || vm.ram_gb < limits.ram || vm.ram_gb > 2048) invalid = ['ram_gb', `Memory must be a whole number from ${limits.ram} to 2048 GB for this role.`];
      else if (!Number.isInteger(vm.disk_gb) || vm.disk_gb < limits.disk || vm.disk_gb > 65536) invalid = ['disk_gb', `Disk size must be a whole number from ${limits.disk} to 65536 GB.`];
      else if (!vm.datastore || !vm.network) invalid = [!vm.datastore ? 'datastore' : 'network', 'Choose a datastore and network for this VM.'];
      else if ((state.inventory?.networks || []).filter(network => network.name === vm.network).length !== 1) invalid = ['network', 'The selected port group is unavailable or ambiguous on this ESXi host. Choose an available port group with a unique name.'];
      else if (vm.ip_mode === 'static') {
        const [address, prefix, extra] = vm.address.split('/');
        if (!validIPv4(address) || extra !== undefined || !/^\d{1,2}$/.test(prefix || '') || Number(prefix) < 1 || Number(prefix) > 30) invalid = ['address', 'Enter an IPv4 address with a subnet prefix from /1 to /30, for example 192.168.1.20/24.'];
        else if (!validIPv4(vm.gateway)) invalid = ['gateway', 'Enter a valid IPv4 default gateway.'];
        else if (!vm.dns.length || vm.dns.length > 4 || vm.dns.some(dns => !validIPv4(dns))) invalid = ['dnsText', 'Enter one to four valid IPv4 DNS servers, separated by commas.'];
      }
      names.add(vm.name);
      if (invalid) return {role: vm.role, key: invalid[0], error: `${roleName(vm.role)}: ${invalid[1]}`};
    }
    return null;
  }
  async function wizardNext() {
    const wizard = state.wizard; if (!wizard || wizard.busy || !state.session || setupRequired()) return;
    wizard.error = ''; wizard.vmError = null;
    if (wizard.step === 0) {
      wizard.name = wizard.name.trim();
      if (!validHostname(wizard.name)) wizard.error = 'Choose a deployment name of 1–63 lowercase letters, numbers, or hyphens, beginning with a letter and ending with a letter or number.';
      else if (!wizard.selected.size) wizard.error = 'Select at least one software role or OS only.';
      else { ensureVMs(); wizard.step = 1; }
      renderWizard(); return;
    }
    if (wizard.step === 1) {
      const invalid = validateVMs();
      if (invalid) { wizard.error = invalid.error; wizard.vmError = invalid; }
      else wizard.step = 2;
      renderWizard();
      if (invalid) {
        const alert = document.getElementById(`vm-error-${invalid.role}`);
        alert?.focus({preventScroll: true}); alert?.scrollIntoView({block: 'center'});
      }
      else $('#wizard-content')?.scrollTo({top: 0});
      return;
    }
    if (isFleetStep()) {
      const saved = await wizard.fleetControls.save();
      if (state.wizard !== wizard || !saved) return;
      wizard.fleetValidated = true; wizard.step++;
      renderWizard(); $('#wizard-content')?.scrollTo({top: 0});
      return;
    }
    if (wizard.selected.has('fleetmanager') && !wizard.fleetValidated) {
      wizard.step = 2; renderWizard(); return;
    }
    if (wizard.selected.has('splunk') && !wizard.accepted) { wizard.error = 'Accept the license terms for the supplied Splunk package before running preflight.'; renderWizard(); return; }
    const session = state.session, epoch = state.routeEpoch;
    const currentRequest = () => state.wizard === wizard && state.session === session && state.routeEpoch === epoch && !wizard.suspended && $('#wizard-dialog').open;
    const autoDeploy = wizard.autoDeploy;
    let submitting = Boolean(wizard.preflight?.ok);
    const spec = getSpec();
    wizard.busy = true; renderWizard();
    try {
      if (!submitting) {
        const result = await api('/api/preflight', {method: 'POST', body: spec});
        if (!currentRequest()) return;
        wizard.preflight = result;
        if (!result.ok) { wizard.error = 'Some checks need attention. Review the results below, update the configuration, and run preflight again.'; return; }
        if (!autoDeploy) return;
        submitting = true; renderWizard();
      }
      if (!currentRequest()) return;
      const result = await api('/api/deployments', {method: 'POST', body: spec});
      if (!currentRequest()) return;
      wizard.busy = false; closeWizard();
      notify('Deployment queued. Follow its progress here.');
      goDetail(result.id);
    } catch (error) { if (currentRequest()) { wizard.error = error.message; if (submitting) wizard.preflight = null; } }
    finally { if (state.wizard === wizard) { wizard.busy = false; renderWizard(); $('#wizard-content')?.scrollTo({top: 0}); } }
  }
  async function poll() {
    if (!state.session || setupRequired() || document.hidden || state.pollBusy) return;
    state.pollBusy = true;
    const epoch = state.routeEpoch;
    try {
      if (state.route === 'detail') await refreshDetail();
      else if (state.route === 'deployments') {
        const revision = state.historyRevision;
        const rows = await api(deploymentListPath());
        if (epoch === state.routeEpoch && revision === state.historyRevision && !state.visibilityBusy.size && !state.stopBusy.size && JSON.stringify(rows) !== JSON.stringify(state.deployments)) {
          const activeElement = document.activeElement;
          const editing = activeElement?.classList.contains('search-field');
          const focusedId = page.contains(activeElement) ? activeElement?.id : '';
          const selection = editing ? activeElement.selectionStart : null;
          state.deployments = rows; renderOverview();
          if (editing) { const input = $('.search-field'); input?.focus({preventScroll: true}); if (selection !== null) input?.setSelectionRange(selection, selection); }
          else if (focusedId) (document.getElementById(focusedId) || (focusedId.startsWith('history-visibility-') ? $('#history-show-hidden') : null))?.focus({preventScroll: true});
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
