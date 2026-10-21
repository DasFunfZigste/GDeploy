'use strict';

window.GDeployPasswordManager = function ({el, button, field, heading, api, copyText, notify, state, page, showLogin}) {
  const dialog = document.getElementById('password-manager-dialog');
  const roleNames = {administrator: 'Administrator', credential_editor: 'Credential Editor', credential_viewer: 'Credential Viewer'};
  const types = ['SSH', 'HTTPS', 'HTTP', 'RDP', 'VNC', 'API', 'OTHER'];
  const cleanups = new Set();
  let secretEpoch = 0;
  const isAdmin = () => (state.session?.role || 'administrator') === 'administrator';
  function clearSecrets() {
    secretEpoch++;
    for (const cleanup of [...cleanups]) cleanup();
    dialog.close(); dialog.replaceChildren();
  }
  function currentView() {
    const session = state.session, epoch = state.routeEpoch;
    return () => state.session === session && state.routeEpoch === epoch;
  }
  function errorBox() { return el('div', {class: 'alert alert-error', role: 'alert', hidden: true}); }
  function error(node, message) { node.textContent = message; node.hidden = !message; }
  function modal(title, body, onSubmit, submitLabel = 'Save entry') {
    dialog.replaceChildren();
    const failure = errorBox();
    const submit = button(submitLabel, 'button-primary'); submit.type = 'submit';
    const cancel = button('Cancel', 'button-ghost', () => dialog.close());
    const form = el('form', {onSubmit: async event => {
      event.preventDefault();
      if (form.dataset.busy || !form.reportValidity()) return;
      form.dataset.busy = 'true'; submit.disabled = true; cancel.disabled = true; error(failure, '');
      const active = () => dialog.open && dialog.contains(form);
      try { await onSubmit(active); }
      catch (caught) { if (active()) error(failure, caught.message); }
      finally { delete form.dataset.busy; submit.disabled = false; cancel.disabled = false; }
    }}, el('div', {class: 'confirm-head'}, el('h2', {id: 'password-manager-dialog-title'}, title)),
    el('div', {class: 'confirm-body'}, body, failure, el('div', {class: 'confirm-actions'}, cancel, submit)));
    dialog.append(form);
    const cleanup = () => {
      for (const input of form.querySelectorAll('input,textarea')) input.value = '';
      cleanups.delete(cleanup);
    };
    cleanups.add(cleanup);
    dialog.showModal();
    form.querySelector('input,select,button')?.focus();
  }
  dialog.addEventListener('cancel', event => { if (dialog.querySelector('form')?.dataset.busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { if (!dialog.open) clearSecrets(); });
  async function copied(value) { notify(await copyText(value) ? 'Copied to clipboard.' : 'Clipboard access was blocked. Select the displayed value and copy it manually.'); }
  function input(id, value = '', options = {}) { return el('input', {id, value, type: 'text', autocomplete: 'off', ...options}); }
  function tabs(users) {
    return el('nav', {class: 'vault-tabs', 'aria-label': 'Password Manager sections'},
      el('a', {href: '#password-manager', class: !users ? 'active' : '', 'aria-current': !users ? 'page' : null}, 'Credentials'),
      isAdmin() ? el('a', {href: '#password-manager/users', class: users ? 'active' : '', 'aria-current': users ? 'page' : null}, 'Users & roles') : null);
  }
  function chrome(users, action) {
    page.replaceChildren(heading('Password Manager', users ? 'Manage who can use your GDeploy workspace and shared credentials.' : 'Your provisioned logins and homelab connections, together.', action), tabs(users));
  }
  async function render(users = false) {
    const current = currentView();
    if (users) return renderUsers(current);
    const catalog = await api('/api/password-manager');
    if (!current()) return;
    chrome(false, catalog.can_edit ? button('Add credential', 'button-primary', () => editCredential(null, current), 'plus') : null);
    const search = input('credential-search', '', {type: 'search', placeholder: 'Search name, application, user or address', 'aria-label': 'Search credentials'});
    const filter = el('select', {id: 'credential-type-filter', 'aria-label': 'Connection type'}, el('option', {value: ''}, 'All connection types'), types.map(type => el('option', {value: type}, type)));
    const source = el('select', {id: 'credential-source-filter', 'aria-label': 'Credential source'}, el('option', {value: ''}, 'All sources'), el('option', {value: 'deployment'}, 'Provisioned by GDeploy'), el('option', {value: 'manual'}, 'Added manually'));
    const list = el('div', {class: 'vault-grid', id: 'credential-list'});
    page.append(el('p', {class: 'alert alert-info'}, 'Editing an entry updates this saved copy only. It does not change a VM or application password. Browser connections are planned.'),
      el('div', {class: 'vault-toolbar'}, search, filter, source, button('Refresh', 'button-small', () => reload(current), 'refresh')),
      el('p', {class: 'muted'}, `Your role: ${roleNames[state.session.role] || 'Administrator'}. Authorized users share this collection.`), list);
    function draw() {
      clearSecrets();
      const query = search.value.toLowerCase();
      const matches = catalog.entries.filter(item => (!filter.value || item.connection_type === filter.value) && (!source.value || item.source === source.value) &&
        [item.title, item.application, item.username, item.host, item.vm_name, item.deployment_name].join(' ').toLowerCase().includes(query));
      list.replaceChildren(...matches.map(item => card(item, catalog.can_edit, current)));
      if (!matches.length) list.append(el('div', {class: 'empty-state'}, el('h2', {}, catalog.entries.length ? 'No matching credentials' : 'No credentials yet'), el('p', {}, catalog.entries.length ? 'Try another search or filter.' : 'Add a homelab entry, or create a deployment to collect its generated credentials here.')));
    }
    search.addEventListener('input', draw); filter.addEventListener('change', draw); source.addEventListener('change', draw); draw();
  }
  async function reload(current) {
    if (!current()) return;
    clearSecrets();
    try { await render(false); } catch (caught) { if (current()) notify(caught.message); }
  }
  function card(item, canEdit, current) {
    const details = el('div', {class: 'vault-secrets', hidden: true});
    const failure = errorBox();
    const reveal = button('Reveal credentials', 'button-small', async () => {
      if (!current()) return;
      if (!details.hidden) { cleanup(); return; }
      const epoch = secretEpoch; reveal.disabled = true; error(failure, '');
      try {
        let data = await api(`/api/password-manager/${encodeURIComponent(item.id)}/reveal`, {method: 'POST'});
        if (!current() || epoch !== secretEpoch || document.hidden) return;
        const secret = (label, value) => value ? el('div', {class: 'vault-secret'}, el('strong', {}, label), el('pre', {tabindex: '0'}, value), button(`Copy ${label.toLowerCase()}`, 'button-small', () => copied(value), 'copy')) : null;
        details.replaceChildren(el('p', {class: 'media-help'}, 'Hidden automatically after 60 seconds or when you leave this page.'), secret('Password', data.password), secret('Private key', data.private_key), data.notes ? el('p', {class: 'vault-notes'}, data.notes) : null,
          !data.password && !data.private_key ? el('p', {}, 'No password or private key stored.') : null);
        data = null;
        details.hidden = false; reveal.textContent = 'Hide credentials';
        cleanups.add(cleanup); timer = setTimeout(cleanup, 60000);
      } catch (caught) { if (current() && epoch === secretEpoch) error(failure, caught.message); }
      finally { reveal.disabled = false; }
    }, 'eye');
    let timer;
    const cleanup = () => { clearTimeout(timer); details.replaceChildren(); details.hidden = true; reveal.textContent = 'Reveal credentials'; cleanups.delete(cleanup); };
    const address = item.host ? `${item.host.includes(':') ? '[' + item.host + ']' : item.host}${item.port ? ':' + item.port : ''}` : 'Address not assigned';
    return el('article', {class: 'surface vault-card', 'data-credential-id': item.id},
      el('div', {class: 'surface-header'}, el('div', {}, el('h2', {}, item.title), el('p', {}, item.application || 'General credential')), el('span', {class: 'status'}, item.connection_type)),
      el('div', {class: 'form-body'}, el('dl', {class: 'vault-metadata'}, el('dt', {}, 'User'), el('dd', {}, item.username || '—'), el('dt', {}, 'IP / hostname'), el('dd', {}, address), el('dt', {}, 'Password'), el('dd', {}, item.has_password ? '••••••••••••' : 'Not stored'),
        item.has_private_key ? [el('dt', {}, 'SSH key'), el('dd', {}, 'Stored securely')] : null),
      el('p', {class: 'media-help'}, item.source === 'deployment' ? `${item.deployment_name} · ${item.vm_name} · Deployment: ${item.deployment_status}` : 'Added manually'),
      item.source === 'deployment' && isAdmin() ? el('a', {href: `#deployment/${encodeURIComponent(item.deployment_id)}`}, 'View deployment') : null,
      failure, details, el('div', {class: 'vault-actions'}, reveal, button('Copy username', 'button-small button-ghost', () => copied(item.username), 'copy'),
        canEdit ? button('Edit', 'button-small', () => editCredential(item, current)) : null,
        canEdit ? button('Delete', 'button-small button-danger', () => deleteCredential(item, current)) : null)));
  }
  async function editCredential(item, current) {
    const epoch = secretEpoch;
    try {
      let initial = item ? await api(`/api/password-manager/${encodeURIComponent(item.id)}/reveal`, {method: 'POST'}) : {};
      if (!current() || epoch !== secretEpoch || document.hidden) return;
      clearSecrets();
      const controls = {
        title: input('credential-title', initial.title || '', {required: true, maxlength: 160}),
        application: input('credential-application', initial.application || '', {maxlength: 160}),
        username: input('credential-username', initial.username || '', {maxlength: 256}),
        password: input('credential-password', initial.password || '', {type: 'password', maxlength: 8192, autocomplete: 'new-password'}),
        host: input('credential-host', initial.host || '', {maxlength: 253, placeholder: '192.168.50.10 or lab-server'}),
        port: input('credential-port', initial.port || '', {type: 'number', min: 1, max: 65535}),
        connection_type: el('select', {id: 'credential-connection-type'}, types.map(type => el('option', {value: type}, type))),
        notes: el('textarea', {id: 'credential-notes', maxlength: 4000, rows: 3}, initial.notes || ''),
        private_key: el('textarea', {id: 'credential-private-key', maxlength: 65536, rows: 5, spellcheck: 'false', autocomplete: 'off'}, initial.private_key || ''),
      };
      controls.connection_type.value = initial.connection_type || 'SSH';
      const revision = initial.revision; initial = null;
      const fields = el('div', {}, el('p', {class: 'alert alert-info'}, 'Saved entry only. These edits do not change the remote machine or application.'),
        field('Entry name', controls.title), el('div', {class: 'field-grid'}, field('Application', controls.application), field('Connection type', controls.connection_type)),
        el('div', {class: 'field-grid'}, field('User', controls.username), field('Password', controls.password, 'An empty field saves an empty password.')),
        el('div', {class: 'field-grid'}, field('IP / hostname', controls.host), field('Port', controls.port, 'Optional.')),
        field('Notes', controls.notes), el('details', {class: 'media-integrity'}, el('summary', {}, 'SSH private key (optional)'), field('Private key', controls.private_key)));
      modal(item ? 'Edit credential' : 'Add credential', fields, async active => {
        if (!current() || !active()) return;
        const body = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]));
        body.port = body.port ? Number(body.port) : null;
        if (item) body.revision = revision;
        await api('/api/password-manager' + (item ? '/' + encodeURIComponent(item.id) : ''), {method: item ? 'PUT' : 'POST', body});
        if (!current() || !active()) return;
        dialog.close(); if (current()) { notify('Credential saved. Remote credentials were not changed.'); await reload(current); }
      });
    } catch (caught) { if (current()) notify(caught.message); }
  }
  function deleteCredential(item, current) {
    clearSecrets();
    modal('Delete saved credential?', el('p', {}, `Remove “${item.title}” from Password Manager? This does not change the remote login. Provisioned credentials remain in the original deployment record and will not be re-added here automatically.`), async active => {
      if (!current()) return;
      await api(`/api/password-manager/${encodeURIComponent(item.id)}?revision=${item.revision}`, {method: 'DELETE'});
      if (!current() || !active()) return;
      dialog.close(); if (current()) { notify('Saved credential removed.'); await reload(current); }
    }, 'Delete credential');
  }
  async function renderUsers(current) {
    const data = await api('/api/users'); if (!current()) return;
    chrome(true, button('Add user', 'button-primary', () => editUser(null, current), 'plus'));
    const list = el('div', {class: 'vault-user-list'});
    page.append(el('section', {class: 'surface'}, el('div', {class: 'form-body'},
      el('h2', {}, 'Access roles'), el('p', {}, 'Administrator: full GDeploy access and user management. Credential Editor: add, edit, delete, reveal and copy shared entries. Credential Viewer: reveal and copy shared entries.'),
      el('p', {class: 'media-help'}, 'New users and password resets require a new password at next sign-in. Disabling, renaming or changing a user revokes their sessions.')), list));
    for (const user of data.users) {
      const self = user.username === state.session.username, protectedUser = user.id === 'primary';
      list.append(el('article', {class: 'vault-user-row'}, el('div', {}, el('strong', {}, user.username), el('p', {class: 'media-help'}, `${roleNames[user.role]} · ${user.enabled ? 'Enabled' : 'Disabled'}${user.must_change_credentials ? ' · Password change required' : ''}${protectedUser ? ' · Instance administrator (protected)' : ''}`)),
        !protectedUser && !self ? el('div', {class: 'vault-actions'}, button('Edit user', 'button-small', () => editUser(user, current)), button('Delete user', 'button-small button-danger', () => {
          clearSecrets(); modal('Delete GUI user?', el('p', {}, `Remove ${user.username} and revoke their sessions? Shared credentials are retained.`), async active => {
            if (!current()) return; await api('/api/users/' + encodeURIComponent(user.id), {method: 'DELETE'});
            if (current() && active()) { dialog.close(); await render(true); }
          }, 'Delete user');
        })) : null));
    }
  }
  function editUser(user, current) {
    clearSecrets();
    const username = input('user-username', user?.username || '', {required: true, minlength: 3, maxlength: 100, pattern: '[A-Za-z0-9][A-Za-z0-9_.\\-]{2,99}'});
    const role = el('select', {id: 'user-role'}, Object.entries(roleNames).map(([value, name]) => el('option', {value}, name))); role.value = user?.role || 'credential_viewer';
    const password = input('user-password', '', {type: 'password', required: !user, minlength: 12, maxlength: 1024, autocomplete: 'new-password'});
    const enabled = input('user-enabled', '', {type: 'checkbox', checked: user ? user.enabled : true});
    modal(user ? 'Edit GUI user' : 'Add GUI user', el('div', {}, field('Username', username), field('Role', role), field(user ? 'New temporary password (optional)' : 'Temporary password', password, user ? 'Leave blank to keep the existing login. A reset requires changing it at next sign-in.' : 'At least 12 characters. The user must choose a new password at first sign-in.'), field('Enabled', enabled)), async active => {
      if (!current()) return;
      await api('/api/users' + (user ? '/' + encodeURIComponent(user.id) : ''), {method: user ? 'PUT' : 'POST', body: {username: username.value, role: role.value, password: password.value, enabled: enabled.checked}});
      if (!current() || !active()) return;
      dialog.close(); if (current()) { notify('GUI user saved.'); await render(true); }
    }, 'Save user');
  }
  function changePassword() {
    const current = currentView(); clearSecrets();
    const previous = input('account-current-password', '', {type: 'password', required: true, autocomplete: 'current-password'});
    const next = input('account-new-password', '', {type: 'password', required: true, minlength: 12, maxlength: 1024, autocomplete: 'new-password'});
    const confirm = input('account-confirm-password', '', {type: 'password', required: true, minlength: 12, maxlength: 1024, autocomplete: 'new-password'});
    modal('Change your GDeploy password', el('div', {}, el('p', {}, 'This changes your GUI login and signs out all your sessions. Saved machine credentials are unchanged.'), field('Current password', previous), field('New password', next), field('Confirm new password', confirm)), async active => {
      if (!current()) return;
      await api('/api/account/password', {method: 'POST', body: {current_password: previous.value, password: next.value, password_confirm: confirm.value}});
      if (!current() || !active()) return;
      const username = state.session.username; dialog.close(); showLogin('', {username, success: 'Password changed. Sign in again.'});
    }, 'Change password');
  }
  return {render, clearSecrets, changePassword};
};
