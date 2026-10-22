'use strict';

window.GDeployPasswordManager = function ({el, button, field, heading, api, copyText, notify, state, page, showLogin}) {
  const dialog = document.getElementById('password-manager-dialog');
  const roleNames = {administrator: 'Administrator', credential_editor: 'Credential Editor', credential_viewer: 'Credential Viewer'};
  const types = ['SSH', 'HTTPS', 'HTTP', 'RDP', 'VNC', 'API', 'OTHER'];
  const cleanups = new Set();
  let secretEpoch = 0;
  let filterSession;
  let filters = {};
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
  function tabs(view) {
    const tab = (name, label, href) => el('a', {href, class: view === name ? 'active' : '', 'aria-current': view === name ? 'page' : null}, label);
    return el('nav', {class: 'vault-tabs', 'aria-label': 'Password Manager sections'},
      tab('credentials', 'Credentials', '#password-manager'), tab('sections', 'Sections', '#password-manager/sections'),
      isAdmin() ? tab('users', 'Users & roles', '#password-manager/users') : null);
  }
  function chrome(view, action) {
    if (typeof view === 'boolean') view = view ? 'users' : 'credentials';
    page.replaceChildren(heading('Password Manager', view === 'users' ? 'Manage who can use your GDeploy workspace and shared credentials.' :
      view === 'sections' ? 'Organize your shared credentials into sections.' : 'Your provisioned logins and homelab connections, together.', action), tabs(view));
  }
  async function render(view = false) {
    const current = currentView();
    if (filterSession !== state.session) { filterSession = state.session; filters = {}; }
    if (view === true) return renderUsers(current);
    const catalog = await api('/api/password-manager');
    if (!current()) return;
    if (view === 'sections') return renderSections(catalog, current);
    chrome(false, catalog.can_edit ? button('Add credential', 'button-primary', () => editCredential(null, current, catalog), 'plus') : null);
    const sectionNames = new Map(catalog.sections.map(section => [section.id, section.name]));
    if (filters.section && filters.section !== 'unsectioned' && !sectionNames.has(filters.section)) filters.section = '';
    if (filters.tag && filters.tag !== ',untagged,' && !catalog.tags.includes(filters.tag)) filters.tag = '';
    const search = input('credential-search', filters.search || '', {type: 'search', placeholder: 'Search credentials, sections or tags', 'aria-label': 'Search credentials'});
    const filter = el('select', {id: 'credential-type-filter', 'aria-label': 'Connection type'}, el('option', {value: ''}, 'All connection types'), types.map(type => el('option', {value: type}, type)));
    const source = el('select', {id: 'credential-source-filter', 'aria-label': 'Credential source'}, el('option', {value: ''}, 'All sources'), el('option', {value: 'deployment'}, 'Provisioned by GDeploy'), el('option', {value: 'manual'}, 'Added manually'));
    const section = el('select', {id: 'credential-section-filter', 'aria-label': 'Section filter'}, el('option', {value: ''}, 'All sections'), el('option', {value: 'unsectioned'}, 'Unsectioned'),
      catalog.sections.map(item => el('option', {value: item.id}, `${item.name} (${item.count})`)));
    const tag = el('select', {id: 'credential-tag-filter', 'aria-label': 'Tag filter'}, el('option', {value: ''}, 'All tags'), el('option', {value: ',untagged,'}, 'Untagged'), catalog.tags.map(name => el('option', {value: name}, name)));
    filter.value = filters.type || ''; source.value = filters.source || ''; section.value = filters.section || ''; tag.value = filters.tag || '';
    const list = el('div', {id: 'credential-list'});
    const count = el('p', {class: 'muted', role: 'status', 'aria-live': 'polite'});
    page.append(el('p', {class: 'alert alert-info'}, 'Editing an entry updates this saved copy only. It does not change a VM or application password. Browser connections are planned.'),
      el('div', {class: 'vault-toolbar'}, search, button('Refresh', 'button-small', () => reload(current), 'refresh')),
      el('div', {class: 'vault-toolbar vault-filters'}, section, tag, filter, source, button('Clear filters', 'button-small button-ghost', () => {
        search.value = ''; section.value = ''; tag.value = ''; filter.value = ''; source.value = ''; draw();
      })),
      el('p', {class: 'muted'}, `Your role: ${roleNames[state.session.role] || 'Administrator'}. Authorized users share this collection. Sections and tags do not change access permissions.`), count, list);
    function choose(kind, value) { (kind === 'section' ? section : tag).value = value; draw(); }
    function draw() {
      clearSecrets();
      filters = {search: search.value, section: section.value, tag: tag.value, type: filter.value, source: source.value};
      const query = search.value.toLowerCase();
      const matches = catalog.entries.filter(item => (!filter.value || item.connection_type === filter.value) && (!source.value || item.source === source.value) &&
        (!section.value || (section.value === 'unsectioned' ? !item.section_id : item.section_id === section.value)) &&
        (!tag.value || (tag.value === ',untagged,' ? !item.tags.length : item.tags.includes(tag.value))) &&
        [item.title, item.application, item.username, item.host, item.vm_name, item.deployment_name, sectionNames.get(item.section_id), ...item.tags].join(' ').toLowerCase().includes(query));
      count.textContent = `${matches.length} of ${catalog.entries.length} credentials`;
      list.replaceChildren();
      for (const group of [...catalog.sections, {id: null, name: 'Unsectioned'}]) {
        const entries = matches.filter(item => item.section_id === group.id);
        if (!entries.length) continue;
        list.append(el('section', {class: 'vault-section-group'}, el('h2', {class: 'vault-group-title'}, group.name, el('span', {class: 'muted'}, ` (${entries.length})`)),
          el('div', {class: 'vault-grid'}, entries.map(item => card(item, catalog.can_edit, current, catalog, choose)))));
      }
      if (!matches.length) list.append(el('div', {class: 'empty-state'}, el('h2', {}, catalog.entries.length ? 'No matching credentials' : 'No credentials yet'), el('p', {}, catalog.entries.length ? 'Try another search or filter.' : 'Add a homelab entry, or create a deployment to collect its generated credentials here.')));
    }
    for (const control of [search, filter, source, section, tag]) control.addEventListener(control === search ? 'input' : 'change', draw);
    draw();
  }
  async function reload(current) {
    if (!current()) return;
    clearSecrets();
    try { await render(false); } catch (caught) { if (current()) notify(caught.message); }
  }
  function organizationFields(value, catalog) {
    const section = el('select', {id: 'credential-section'}, el('option', {value: ''}, 'Unsectioned'),
      catalog.sections.map(item => el('option', {value: item.id}, item.name)), el('option', {value: '__new__'}, '+ Create a new section'));
    section.value = value.section_id || '';
    const newName = input('credential-new-section', '', {maxlength: 80, placeholder: 'For example: Network or Lab servers'});
    const newField = field('New section name', newName); newField.hidden = true;
    section.addEventListener('change', () => { newField.hidden = section.value !== '__new__'; newName.required = !newField.hidden; if (!newField.hidden) newName.focus(); });
    const tags = input('credential-tags', (value.tags || []).join(', '), {maxlength: 1200, placeholder: 'homelab, linux, production'});
    const body = el('div', {}, field('Section', section, 'One section per credential. Sections organize the shared collection.'), newField,
      field('Tags', tags, 'Separate tags with commas. Up to 20 tags, 40 characters each; tags are normalized to lowercase.'));
    async function values(current, active) {
      if (section.value === '__new__') {
        const created = await api('/api/password-manager/sections', {method: 'POST', body: {name: newName.value}});
        if (!current() || !active()) return null;
        section.insertBefore(el('option', {value: created.id}, created.name), section.lastElementChild);
        section.value = created.id; newName.value = ''; newName.required = false; newField.hidden = true;
      }
      return {section_id: section.value || null, tags: tags.value.split(',').map(value => value.trim()).filter(Boolean)};
    }
    return {body, values};
  }
  function organizeCredential(item, current, catalog) {
    clearSecrets();
    const fields = organizationFields(item, catalog);
    modal('Organize credential', el('div', {}, el('p', {}, item.title), fields.body), async active => {
      if (!current() || !active()) return;
      const values = await fields.values(current, active); if (!values || !current() || !active()) return;
      await api(`/api/password-manager/${encodeURIComponent(item.id)}/organization`, {method: 'PATCH', body: {...values, revision: item.revision}});
      if (!current() || !active()) return;
      dialog.close(); notify('Credential organization saved.'); await reload(current);
    }, 'Save organization');
  }
  function renderSections(catalog, current) {
    chrome('sections', catalog.can_edit ? button('Add section', 'button-primary', () => editSection(null, current), 'plus') : null);
    const list = el('div', {class: 'vault-section-list'});
    page.append(el('p', {class: 'alert alert-info'}, 'Sections group credentials; tags can span several sections. All authorized users still share this collection. Removing a section keeps its credentials under Unsectioned.'), list);
    const rows = [...catalog.sections, {id: null, name: 'Unsectioned', count: catalog.entries.filter(item => !item.section_id).length}];
    for (const section of rows) {
      const view = button('View credentials', 'button-small', () => { filters = {section: section.id || 'unsectioned'}; location.hash = 'password-manager'; });
      list.append(el('article', {class: 'surface vault-section-row', 'data-section-id': section.id || 'unsectioned'},
        el('div', {}, el('h2', {}, section.name), el('p', {class: 'muted'}, `${section.count} credential${section.count === 1 ? '' : 's'}`)),
        el('div', {class: 'vault-actions'}, view,
          section.id && catalog.can_edit ? button('Rename', 'button-small', () => editSection(section, current)) : null,
          section.id && catalog.can_edit ? button('Remove section', 'button-small button-danger', () => removeSection(section, current)) : null)));
    }
  }
  function editSection(section, current) {
    clearSecrets();
    const name = input('section-name', section?.name || '', {required: true, maxlength: 80});
    modal(section ? 'Rename section' : 'Add section', field('Section name', name), async active => {
      if (!current() || !active()) return;
      await api('/api/password-manager/sections' + (section ? '/' + encodeURIComponent(section.id) : ''), {
        method: section ? 'PUT' : 'POST', body: {name: name.value, ...(section ? {revision: section.revision} : {})}});
      if (!current() || !active()) return;
      dialog.close(); notify('Section saved.'); await render('sections');
    }, 'Save section');
  }
  function removeSection(section, current) {
    clearSecrets();
    modal('Remove section?', el('p', {}, `Remove “${section.name}”? All credentials in it will be kept under Unsectioned, with their tags and secrets unchanged.`), async active => {
      if (!current() || !active()) return;
      await api(`/api/password-manager/sections/${encodeURIComponent(section.id)}?revision=${section.revision}`, {method: 'DELETE'});
      if (!current() || !active()) return;
      dialog.close(); notify('Section removed. Credentials retained.'); await render('sections');
    }, 'Remove section');
  }
  function card(item, canEdit, current, catalog, choose) {
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
      el('div', {class: 'vault-labels'}, button(catalog.sections.find(section => section.id === item.section_id)?.name || 'Unsectioned', 'vault-label vault-section-label', () => choose('section', item.section_id || 'unsectioned')),
        item.tags.map(tag => { const control = button(tag, 'vault-label', () => choose('tag', tag)); control.setAttribute('aria-label', `Filter tag: ${tag}`); return control; })),
      failure, details, el('div', {class: 'vault-actions'}, reveal, button('Copy username', 'button-small button-ghost', () => copied(item.username), 'copy'),
        canEdit ? button('Organize', 'button-small', () => organizeCredential(item, current, catalog)) : null,
        canEdit ? button('Edit', 'button-small', () => editCredential(item, current, catalog)) : null,
        canEdit ? button('Delete', 'button-small button-danger', () => deleteCredential(item, current)) : null)));
  }
  async function editCredential(item, current, catalog) {
    const epoch = secretEpoch;
    try {
      let initial = item ? await api(`/api/password-manager/${encodeURIComponent(item.id)}/reveal`, {method: 'POST'}) : {section_id: catalog.sections.some(section => section.id === filters.section) ? filters.section : null};
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
      const organization = organizationFields(initial, catalog);
      const revision = initial.revision; initial = null;
      const fields = el('div', {}, el('p', {class: 'alert alert-info'}, 'Saved entry only. These edits do not change the remote machine or application.'),
        field('Entry name', controls.title), organization.body, el('div', {class: 'field-grid'}, field('Application', controls.application), field('Connection type', controls.connection_type)),
        el('div', {class: 'field-grid'}, field('User', controls.username), field('Password', controls.password, 'An empty field saves an empty password.')),
        el('div', {class: 'field-grid'}, field('IP / hostname', controls.host), field('Port', controls.port, 'Optional.')),
        field('Notes', controls.notes), el('details', {class: 'media-integrity'}, el('summary', {}, 'SSH private key (optional)'), field('Private key', controls.private_key)));
      modal(item ? 'Edit credential' : 'Add credential', fields, async active => {
        if (!current() || !active()) return;
        const body = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]));
        body.port = body.port ? Number(body.port) : null;
        if (item) body.revision = revision;
        const values = await organization.values(current, active); if (!values || !current() || !active()) return;
        Object.assign(body, values);
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
