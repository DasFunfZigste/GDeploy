import re
from html.parser import HTMLParser

import pytest

from gdeploy import __version__


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stylesheets = []
        self.scripts = []
        self.text = []
        self.excluded = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs.get("href"))
        if tag == "script":
            self.scripts.append(attrs.get("src"))
        if tag in {"script", "style"}:
            self.excluded += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.excluded -= 1

    def handle_data(self, data):
        if not self.excluded:
            self.text.append(data)


def test_home_page_stamps_both_assets_and_displays_running_version(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    page = PageAssets()
    page.feed(response.text)
    assert f"/static/app.js?v={__version__}" in page.scripts
    assert f"/static/app.css?v={__version__}" in page.stylesheets
    assert re.search(rf"\bv?{re.escape(__version__)}\b", " ".join(page.text))
    assert "__GDEPLOY_VERSION__" not in response.text
    # Both assets must be available before sign-in so cached scripts cannot strand the login page.
    for url in page.scripts + page.stylesheets:
        assert client.get(url).status_code == 200


@pytest.mark.parametrize("path", [
    "/",
    "/static/app.js",
    "/static/app.css",
    f"/static/app.js?v={__version__}",
    f"/static/app.css?v={__version__}",
])
def test_page_and_assets_require_cache_revalidation(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("asset", ["app.js", "app.css"])
@pytest.mark.parametrize("validator,request_header", [
    ("ETag", "If-None-Match"),
    ("Last-Modified", "If-Modified-Since"),
])
def test_conditional_asset_responses_keep_revalidation_policy(client, asset, validator, request_header):
    url = f"/static/{asset}?v={__version__}"
    original = client.get(url)
    assert original.status_code == 200
    cached = client.get(url, headers={request_header: original.headers[validator]})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["Cache-Control"] == "no-cache"


def test_missing_static_asset_cannot_be_heuristically_cached(client):
    response = client.get("/static/missing-file.js")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-cache"


def test_ui_cache_policy_does_not_change_api_health_or_authentication(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["Cache-Control"] == "no-store"
    settings = client.get("/api/settings")
    assert settings.status_code == 401
    assert settings.headers["Cache-Control"] == "no-store"


def test_fleetmanager_version_picker_loads_repository_options_without_saving_credentials(client):
    script = client.get("/static/app.js").text
    assert "const onlineVersion = el('select', {id: 'fleetmanager-online-version'" in script
    assert "el('option', {value: ''}, 'Latest available')" in script
    assert "versions.map(version => el('option', {value: version}, version))" in script
    loader = script.split("async function loadVersions()", 1)[1].split("function renderCatalog(", 1)[0]
    assert "api(`${endpoint}/versions`, {method: 'POST', body: {repository_token: draftToken}})" in loader
    assert "form.reportValidity()" not in loader
    assert "community.value" not in loader and "license.files" not in loader
    assert "method: 'PUT'" not in loader
    assert "id = 'fleetmanager-load-versions'" in script
    assert "Loading versions does not save your token or other setup changes." in script


def test_fleetmanager_ui_saves_restores_and_clears_online_version_for_offline(client):
    script = client.get("/static/app.js").text
    panel = script.split("function createFleetManagerPanel(", 1)[1].split("function createSSHAccessPanel(", 1)[0]
    assert "renderVersions(next.online_version || '')" in panel
    assert "onlineVersion.disabled = locked || mode !== 'online'" in panel
    assert "online_version: mode === 'online' ? onlineVersion.value.trim() : ''" in panel
    assert "onlineVersion.addEventListener('change'" in panel
    assert "options.onDirty?.()" in panel
    assert "renderCatalog(next, !catalog)" in panel  # Refresh preserves an edited draft.
    assert "onDirty: () => { wizard.fleetValidated = false; invalidatePreflight(); }" in script


def test_fleetmanager_version_lookup_preserves_pins_and_ignores_stale_responses(client):
    script = client.get("/static/app.js").text
    picker = script.split("function renderVersions(", 1)[1].split("function renderCatalog(", 1)[0]
    assert "if (selected && !versions.includes(selected))" in picker
    assert "saved selection · not refreshed" in picker
    assert "onlineVersion.value = selected" in picker
    assert "Your current selection has been kept." in picker
    assert "Existing choices may be outdated." in picker
    assert "mode !== 'online') return" in picker
    assert "state.session === session && request === versionRequest" in picker
    assert "token.value === draftToken" in picker
    assert "if (!currentRequest()) return" in picker
    assert "versionsPending = false" in picker
    assert "if (!value && versionsPending) queueMicrotask(loadPendingVersions)" in script
    assert "!panel.closest('[hidden]')" in picker
    assert "busy = 'versions'; if (options.wizard) onBusy(true)" in picker
    assert "fleetControls.activate()" in script


def test_delayed_api_unauthorized_response_cannot_clear_a_new_session(client):
    script = client.get("/static/app.js").text
    request = script.split("async function api(", 1)[1].split("function notify(", 1)[0]
    assert "const requestSession = state.session" in request
    assert "response.status === 401 && path !== '/api/login' && state.session === requestSession" in request


def test_fleetmanager_version_is_shown_in_setup_and_preflight_review(client):
    script = client.get("/static/app.js").text
    assert "id: 'fleetmanager-saved-version'}, fleetManagerVersionSummary(next)" in script
    assert "wizard.fleetCatalog = catalog" in script
    review = script.split("function renderReview(content)", 1)[1].split("const validHostname", 1)[0]
    assert "fleetManagerVersionSummary(fleet)" in review
    assert review.index("id: 'fleetmanager-review'") < review.index("'Preflight checks'")
    assert "Version: ${catalog.online_version} (exact)." in script
    assert "Version: latest available in the repository." in script
    assert "Version: ${selected.version} from the selected .deb package." in script


def test_new_wizard_requires_intentional_role_selection(client):
    script = client.get("/static/app.js").text
    initializer = script.split("else state.wizard = ", 1)[1].split(";", 1)[0]
    assert "selected: new Set()" in initializer
    assert "autoDeploy: false" in initializer
    assert "wizard.step === 0 && !wizard.selected.size" in script
    assert "if (role === 'kibana' && event.target.checked) wizard.selected.add('elasticsearch')" in script
    assert "else if (!wizard.selected.size) wizard.error" in script


def test_auto_deploy_is_explicit_and_waits_for_successful_current_preflight(client):
    script = client.get("/static/app.js").text
    assert "id: 'wizard-auto-deploy'" in script
    assert "'Run preflight & deploy'" in script
    assert "wizard.autoDeploy = event.target.checked; invalidatePreflight(); renderWizard()" in script
    submit = script.split("async function wizardNext()", 1)[1].split("async function poll()", 1)[0]
    assert "wizard.busy || !state.session || setupRequired()" in submit
    assert "state.wizard === wizard && state.session === session && state.routeEpoch === epoch" in submit
    assert "if (!result.ok) { wizard.error" in submit
    assert "if (!autoDeploy) return" in submit
    assert submit.index("api('/api/preflight'") < submit.index("api('/api/deployments'")
    assert submit.count("api('/api/deployments'") == 1
    assert "if (submitting) wizard.preflight = null" in submit


def test_log_copy_uses_truthful_fallback_and_keeps_full_manual_snapshot_selectable(client):
    script = client.get("/static/app.js").text
    fallback = script.split("function copyTextFallback(", 1)[1].split("function globalError(", 1)[0]
    assert "document.execCommand('copy') === true" in fallback
    assert "temporary.remove()" in fallback
    assert "focused.setSelectionRange(...inputSelection)" in fallback
    assert "selection.addRange(range)" in fallback
    assert "await navigator.clipboard.writeText(text); return true" in fallback
    assert "return copyTextFallback(text)" in fallback
    detail = script.split("function renderDetail(", 1)[1].split("function renderProgress(", 1)[0]
    assert "`Deployment: ${data.name}`" in detail
    assert "`Status: ${statusNames[data.status] || data.status}`" in detail
    assert "`Error: ${data.error}`" in detail
    assert "if (copied) notify('Deployment logs copied.')" in detail
    assert "manualText.value = text; manualCopy.hidden = false" in detail
    assert "manualText.select()" in detail
    assert "id: 'deployment-log-manual-text'" in detail
    assert "if (!open) { manualCopy.hidden = true; manualText.value = ''; }" in detail
    assert "$('#deployment-log-manual-copy')?.hidden === false" in script
    assert "const selectingLogs = state.openLogs.has(id) && selection" in script
    assert "!selectingLogs && !copyingLogs" in script
