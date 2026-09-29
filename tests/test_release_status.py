import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture
def status(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "release_status", Path(__file__).resolve().parents[1] / "scripts/release_status.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("RELEASE_TAG", "v0.1.1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "DasFunfZigste/GDeploy")
    monkeypatch.setenv("GH_TOKEN", "test-only-token")
    return module


@pytest.mark.parametrize("releases,expected", [([], "missing"), ([{"tag_name": "v0.1.1", "draft": True}], "draft")])
def test_drafts_are_found_in_authenticated_listing(status, monkeypatch, capsys, releases, expected):
    def response(request, timeout):
        assert "/releases?per_page=100&page=1" in request.full_url
        assert request.headers["Authorization"] == "Bearer test-only-token"
        return io.StringIO(json.dumps(releases))

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    status.main()
    assert capsys.readouterr().out.strip() == expected


def test_published_release_cannot_be_modified(status, monkeypatch):
    monkeypatch.setattr(
        status.urllib.request,
        "urlopen",
        lambda *a, **k: io.StringIO(json.dumps([{"tag_name": "v0.1.1", "draft": False}])),
    )
    with pytest.raises(SystemExit, match="never overwritten"):
        status.main()


@pytest.mark.parametrize("code", [403, 404, 429, 500])
def test_api_errors_never_masquerade_as_missing_release(status, monkeypatch, code):
    def response(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.github.com/", code, "test error", {}, None)

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    with pytest.raises(SystemExit, match=f"HTTP {code}"):
        status.main()


def test_older_draft_is_found_across_pages(status, monkeypatch, capsys):
    pages = []

    def response(request, timeout):
        pages.append(request.full_url)
        records = (
            [{"tag_name": f"v1.0.{n}", "draft": False} for n in range(100)]
            if len(pages) == 1
            else [{"tag_name": "v0.1.1", "draft": True}]
        )
        return io.StringIO(json.dumps(records))

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    status.main()
    assert len(pages) == 2 and pages[-1].endswith("page=2")
    assert capsys.readouterr().out.strip() == "draft"
