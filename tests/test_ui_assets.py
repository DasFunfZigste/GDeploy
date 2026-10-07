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


def test_fleetmanager_version_field_accepts_exact_debian_versions(client):
    script = client.get("/static/app.js").text
    declaration = re.search(r"const onlineVersion = el\('input', \{(.+?)\}\);", script).group(1)
    assert "id: 'fleetmanager-online-version'" in declaration
    assert "maxlength: 128" in declaration
    assert "required:" not in declaration
    pattern = re.search(r"pattern: '([^']+)'", declaration).group(1).replace("\\\\", "\\")
    for version in ("29.2.2", "29.2.2-1", "1:29.2.2-1", "29.2.2~rc1+build.1"):
        assert re.fullmatch(pattern, version), version
    for version in ("latest", "v29.2.2", "29.*", "29.2.2:1", "29.2.2 --option", "$(command)"):
        assert not re.fullmatch(pattern, version), version
    assert "'Version to install · optional', onlineVersion, onlineVersionHelp" in script
    assert "Version availability is checked on the VM during installation." in script


def test_fleetmanager_ui_saves_restores_and_clears_online_version_for_offline(client):
    script = client.get("/static/app.js").text
    panel = script.split("function createFleetManagerPanel(", 1)[1].split("function createSSHAccessPanel(", 1)[0]
    assert "onlineVersion.value = next.online_version || ''" in panel
    assert "onlineVersion.disabled = locked || mode !== 'online'" in panel
    assert "online_version: mode === 'online' ? onlineVersion.value.trim() : ''" in panel
    assert "[community, token, onlineVersion, license]" in panel
    assert "options.onDirty?.()" in panel
    assert "renderCatalog(next, !catalog)" in panel  # Refresh preserves an edited draft.
    assert "onDirty: () => { wizard.fleetValidated = false; invalidatePreflight(); }" in script


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
