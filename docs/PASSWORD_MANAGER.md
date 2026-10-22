# Password Manager

Open **Password Manager** in the left sidebar. It collects OS and application logins from existing and new GDeploy deployments, including hidden history. You can also select **Add credential** to save your own homelab logins.

Each entry has an editable name, user, password, application, IP address or hostname, optional port, connection type, notes and optional SSH private key. Connection types are SSH, HTTPS, HTTP, API, RDP, VNC and Other. Search by name, application, user or address, or filter by connection type and source.

**Edits change the saved Password Manager copy only.** They do not change passwords, SSH keys or configuration on a remote machine, and do not change the original credentials used by a queued/running deployment. If you change a password yourself on a machine, edit its saved entry here to match. An empty password/key field deliberately clears that field.

## Sections and tags

Use **Password Manager → Sections → Add section** to create groups such as Network, Servers or Applications. Sections can be created before adding entries, renamed later, or removed. Removing a section keeps all its credentials under **Unsectioned**, with passwords, keys and tags intact. An entry belongs to one section at a time, or Unsectioned.

When adding or editing a credential, choose a section or **+ Create a new section** to add one without leaving the form. Enter any number of comma-separated tags up to 20 per credential, for example `homelab, linux, production`. Each tag is limited to 40 characters; extra spaces and duplicate tags are removed, and tags are saved in lowercase. To remove a tag, delete it from the entry's Tags field; an empty field clears all tags. Unused tags disappear from the filter automatically.

**Organize** changes just an entry's section and tags without retrieving its password or key. Administrators and Credential Editors can organize entries and manage sections. Credential Viewers can browse sections and filter entries.

The Credentials tab groups entries by section. Select **All sections**, **Unsectioned** or an individual section; combine that with the tag, connection-type and source filters or text search. Click a tag on a card to filter by it. **Clear filters** returns to all entries. Filters remain while you work in this browser session and reset on sign-out/reload. The Sections tab's **View credentials** opens that section. All organization changes persist across container updates/restarts and are preserved when provisioning discovers new addresses or service details. Existing entries start Unsectioned without tags.

Sections and tags organize the shared collection; they do not provide private folders or change who may reveal a credential. Section names and tags use the existing encryption key, and label values are not written to audit messages. The `credential_sections` table is additive in v0.21.0; older releases do not expose this organization UI. Keep the original volume/key and a matching backup when upgrading or rolling back.

## Provisioned credentials

GDeploy collects generated OS logins and automation SSH keys, Splunk administrator credentials, Elasticsearch/Kibana logins, the Kibana service-account token, and returned FleetManager administrator credentials. Corelight service community strings exposed by the existing deployment credential view are saved as API entries, with the community string in the password field. Installation inputs such as vendor repository tokens, licenses and application encryption keys retain their existing protected configuration storage; they are not login entries.

Entries can appear as soon as credentials are generated, before a guest is installed or receives its address. The deployment status identifies queued, failed, stopped or reverted work; an entry alone does not establish that a service is running. GDeploy fills addresses and returned service credentials as provisioning progresses. Later job updates preserve fields you have edited in Password Manager.

Hiding a deployment retains its entries. Removing deployment resources retains their saved credentials and shows the deployment's current status. Deleting an entry removes it from Password Manager without deleting the VM or changing its login. A deleted provisioned entry is not automatically recreated on restart; original job credentials remain in the deployment's administrator-only **Credentials** view. A new deployment or redeployment receives its own entries.

## Users and roles

An administrator opens **Password Manager → Users & roles → Add user**. Choose a username, role and temporary password of at least 12 characters. The user must choose a different password at first sign-in. Give the temporary password to that user through your normal private channel.

| Role | Password Manager | Other GDeploy sections | Manage GUI users |
| --- | --- | --- | --- |
| Administrator | Add, edit, delete, reveal and copy | Full access | Yes |
| Credential Editor | Add, edit, delete, reveal and copy | No | No |
| Credential Viewer | Reveal and copy | No | No |

All authorized roles share the same credential collection. This release does not provide private entries, per-entry sharing or custom permission definitions. Permissions are enforced by the API as well as the UI; changing a URL does not bypass them.

Use **Edit user** to change a role or username, disable access, or set a new temporary password. Leaving the reset field blank keeps the current password. Account edits and deletion revoke that user's sessions; a password reset requires replacement at next sign-in. The original instance administrator is protected from deletion/demotion. Administrators cannot remove or change their own role through user management.

Use the key button beside your account in the sidebar, or the key button in the mobile header, to **Change your password**. Enter your current password and confirm a new one. This changes the GUI login and signs out its sessions; machine credentials remain unchanged.

## Storage and visibility

All entry fields, notes, private keys and source snapshots are encrypted with the existing instance encryption key in the persistent data volume. GUI passwords use salted scrypt hashes. Ordinary credential lists contain masked presence flags; passwords and keys are retrieved only for explicit reveal or editing. Reveals and changes record the acting username and record identifier in the audit log without credential values.

Revealed secrets hide after 60 seconds and clear on navigation, sign-out, or hiding/leaving the browser page. Editing dialogs also clear when leaving/hiding the page. Clipboard contents are controlled by your browser/OS; GDeploy cannot erase something already copied. API credential responses disable caching. No browser local storage is used for credentials.

Back up the **complete data volume and original encryption configuration**, including `bootstrap.json` for automatic installations, alongside the database, media and any existing environment files. Do not regenerate the key. New `gui_users` and `credential_entries` tables are additive. Versions before v0.20.0 do not expose this manager or additional users, and older workers do not collect entries while running; a later v0.20.0 startup imports their recorded logins. Preserve the matching backup and finish/resolve active work before rollback. Password Manager edits never repair a failed guest or rotate a live password.

## Future connections

This release stores connection metadata and credentials. Embedded SSH terminals, VNC, RDP and hypervisor consoles remain future work in [VM Manager](ROADMAP.md). There is no browser connection/proxy operation yet.
