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


@pytest.mark.parametrize("release,expected", [(None, "missing"), ({"tagName": "v0.1.1", "isDraft": True}, "draft")])
def test_direct_tag_lookup_detects_missing_or_draft_release(status, monkeypatch, capsys, release, expected):
    calls = []

    def response(request, timeout):
        calls.append(request)
        assert request.full_url == "https://api.github.com/graphql"
        assert request.get_method() == "POST"
        assert request.headers["Authorization"] == "Bearer test-only-token"
        assert request.get_header("Content-type") == "application/json"
        payload = json.loads(request.data)
        assert payload["variables"] == {"owner": "DasFunfZigste", "name": "GDeploy", "tag": "v0.1.1"}
        assert "release(tagName: $tag)" in payload["query"]
        assert timeout == 30
        return io.StringIO(json.dumps({"data": {"repository": {"release": release}}}))

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    status.main()
    assert len(calls) == 1
    assert capsys.readouterr().out.strip() == expected


def test_published_release_cannot_be_modified(status, monkeypatch):
    monkeypatch.setattr(
        status.urllib.request,
        "urlopen",
        lambda *a, **k: io.StringIO(json.dumps({"data": {"repository": {
            "release": {"tagName": "v0.1.1", "isDraft": False},
        }}})),
    )
    with pytest.raises(SystemExit, match="never overwritten"):
        status.main()


@pytest.mark.parametrize("code", [403, 404, 429, 500])
def test_api_errors_never_masquerade_as_missing_release(status, monkeypatch, capsys, code):
    def response(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.github.com/", code, "test error", {}, None)

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    with pytest.raises(SystemExit, match=f"HTTP {code}"):
        status.main()
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "result",
    [
        {"errors": [{"message": "Access denied"}]},
        {"data": {"repository": {"release": None}}, "errors": [{"message": "Partial failure"}]},
        {"data": {"repository": {"release": {"tagName": "v0.1.1", "isDraft": True}}},
         "errors": [{"message": "Partial failure"}]},
        {"data": {"repository": None}},
        {"data": None},
        {"data": {"repository": {}}},
        {},
        [],
        {"data": {"repository": {"release": {}}}},
        {"data": {"repository": {"release": {"tagName": "v0.1.1", "isDraft": "true"}}}},
        {"data": {"repository": {"release": {"tagName": "v0.1.1", "isDraft": 1}}}},
        {"data": {"repository": {"release": {"tagName": "v0.1.0", "isDraft": True}}}},
        {"data": {"repository": {"release": []}}},
    ],
)
def test_graphql_errors_and_malformed_responses_fail_closed(status, monkeypatch, capsys, result):
    monkeypatch.setattr(status.urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps(result)))
    with pytest.raises(SystemExit, match="Could not verify release state"):
        status.main()
    assert not capsys.readouterr().out


@pytest.mark.parametrize("error", [urllib.error.URLError("connection failed"), TimeoutError("timed out")])
def test_network_errors_fail_closed(status, monkeypatch, capsys, error):
    def response(*args, **kwargs):
        raise error

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    with pytest.raises(SystemExit, match="network error"):
        status.main()
    assert not capsys.readouterr().out


def test_invalid_json_fails_closed(status, monkeypatch, capsys):
    monkeypatch.setattr(status.urllib.request, "urlopen", lambda *a, **k: io.StringIO("not-json"))
    with pytest.raises(SystemExit, match="invalid JSON"):
        status.main()
    assert not capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("RELEASE_TAG", "v0.1.1;bad"), ("GITHUB_REPOSITORY", "owner/repo/other")])
def test_invalid_request_inputs_do_not_call_api(status, monkeypatch, key, value):
    monkeypatch.setenv(key, value)

    def response(*args, **kwargs):
        pytest.fail("Invalid input must be rejected before an API request")

    monkeypatch.setattr(status.urllib.request, "urlopen", response)
    with pytest.raises(SystemExit, match="Invalid"):
        status.main()
