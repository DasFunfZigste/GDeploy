import asyncio
import io
import ssl
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
import requests
from pyVmomi import vim
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from gdeploy import vmware
from gdeploy.vmware import ESXiClient, VMwareError


Browser = vim.host.DatastoreBrowser


class BrowserStub:
    def __init__(self):
        self.calls = []
        self.entries = []
        self.error = None

    def InvokeMethod(self, obj, method, args):
        self.calls.append((method.wsdlName, args))
        result = Browser.SearchResults(folderPath=args[0], file=self.entries)
        return NS(info=NS(state="error" if self.error else "success", result=result, error=self.error))


@pytest.fixture
def esxi():
    instance = ESXiClient("esxi.example.test", "root", "private-password")
    instance._si = NS(_stub=NS(cookie='vmware_soap_session="private-session"; Path=/; HttpOnly; Secure'))
    instance._content = NS(fileManager=MagicMock())
    instance._dc = NS(name="ha-datacenter")
    stub = BrowserStub()
    instance._host = NS(datastore=[NS(
        name="ISO Datastore", summary=NS(accessible=True, maintenanceMode="normal"),
        browser=Browser("browser-1", stub),
    )])
    instance._browser_stub = stub
    instance._http = MagicMock()
    response = instance._http.get.return_value
    response.status_code = 200
    response.headers = {"Content-Length": "8"}
    response.raw.read1.side_effect = [b"inst", b"all", b"r", b""]
    return instance


def test_browse_uses_nonrecursive_real_sdk_search_types_and_sorts_results(esxi):
    esxi._browser_stub.entries = [
        Browser.IsoImageInfo(path="zebra.ISO", fileSize=10),
        Browser.FolderInfo(path="Ubuntu images"),
        Browser.FileInfo(path="Alpha.iso", fileSize=20),
        Browser.FolderInfo(path="Archive/"),
        Browser.FileInfo(path="disk.vmdk", fileSize=200),
        Browser.VmDiskInfo(path="fake.iso", fileSize=10),
    ]
    result = esxi.browse_iso_media("ISO Datastore")
    assert result == {
        "datastore": "ISO Datastore", "folder": "", "parent": None,
        "folders": [{"name": "Archive", "path": "Archive"}, {"name": "Ubuntu images", "path": "Ubuntu images"}],
        "files": [{"name": "Alpha.iso", "path": "Alpha.iso", "size_bytes": 20},
                  {"name": "zebra.ISO", "path": "zebra.ISO", "size_bytes": 10}],
    }
    method, (path, spec) = esxi._browser_stub.calls[0]
    assert method == "SearchDatastore_Task"
    assert path == "[ISO Datastore] "
    assert isinstance(spec, Browser.SearchSpec)
    assert [type(query) for query in spec.query] == [Browser.FolderQuery, Browser.IsoImageQuery]
    assert spec.details.fileType is True
    assert spec.details.fileSize is True
    assert spec.searchCaseInsensitive is True
    # A '*.iso' match pattern would accidentally hide navigation folders.
    assert not spec.matchPattern
    esxi._content.fileManager.MakeDirectory.assert_not_called()
    esxi._content.fileManager.DeleteDatastoreFile_Task.assert_not_called()


def test_browse_handles_spaces_unicode_and_parent_navigation(esxi):
    esxi._browser_stub.entries = [Browser.IsoImageInfo(path="Install Média.ISO", fileSize=40)]
    result = esxi.browse_iso_media("ISO Datastore", "Install Media/Édition")
    assert result["folder"] == "Install Media/Édition"
    assert result["parent"] == "Install Media"
    assert result["files"] == [{"name": "Install Média.ISO", "path": "Install Media/Édition/Install Média.ISO", "size_bytes": 40}]
    assert esxi.browse_iso_media("ISO Datastore", "Install Media")["parent"] == ""


@pytest.mark.parametrize("path", [
    "/", "/isos", "..", ".", "isos/../outside", "isos//more", "isos/", "isos\\more",
    "[other] images", "isos/%2e%2e", "isos/%252e%252e", "isos\nmore", "isos\x7f", "isos\u0085", None,
])
def test_browse_rejects_unsafe_folder_paths_before_sdk_request(esxi, path):
    with pytest.raises(VMwareError):
        esxi.browse_iso_media("ISO Datastore", path)
    assert esxi._browser_stub.calls == []


def test_browse_omits_unsafe_remote_entries_and_unknown_sizes(esxi):
    esxi._browser_stub.entries = [
        Browser.FolderInfo(path=".."), Browser.FolderInfo(path="../escape"), Browser.FolderInfo(path=""),
        Browser.FileInfo(path="nested/outside.iso", fileSize=2),
        Browser.IsoImageInfo(path="%2e%2e.iso", fileSize=2),
        Browser.IsoImageInfo(path="no-size.iso"),
        Browser.IsoImageInfo(path="negative.iso", fileSize=-1),
        Browser.IsoImageInfo(path="good.iso", fileSize=8),
    ]
    result = esxi.browse_iso_media("ISO Datastore")
    assert result["folders"] == []
    assert result["files"] == [{"name": "good.iso", "path": "good.iso", "size_bytes": 8}]


def test_browse_omits_identifiable_link_types(esxi):
    class SymlinkFolderInfo(Browser.FolderInfo):
        pass

    class SymlinkFileInfo(Browser.IsoImageInfo):
        pass

    esxi._browser_stub.entries = [
        SymlinkFolderInfo(path="linked-folder"), SymlinkFileInfo(path="linked.iso", fileSize=8),
        Browser.FolderInfo(path="regular-folder"),
    ]
    result = esxi.browse_iso_media("ISO Datastore")
    assert result["files"] == []
    assert result["folders"] == [{"name": "regular-folder", "path": "regular-folder"}]


def test_browse_rejects_ambiguous_duplicate_paths(esxi):
    esxi._browser_stub.entries = [Browser.IsoImageInfo(path="same.iso", fileSize=8)] * 2
    with pytest.raises(VMwareError, match="duplicate"):
        esxi.browse_iso_media("ISO Datastore")


def test_browse_has_bounded_result_count(esxi):
    esxi._browser_stub.entries = [Browser.IsoImageInfo(path=f"{i}.iso", fileSize=1) for i in range(1001)]
    with pytest.raises(VMwareError, match="more than 1000"):
        esxi.browse_iso_media("ISO Datastore")


def test_browse_sanitizes_permission_faults(esxi):
    esxi._browser_stub.error = vim.fault.NoPermission(msg="Missing Datastore.Browse; private-password private-session")
    with pytest.raises(VMwareError, match="NoPermission") as exc:
        esxi.browse_iso_media("ISO Datastore")
    assert "Datastore.Browse" in str(exc.value)
    assert "private-password" not in str(exc.value)
    assert "private-session" not in str(exc.value)


def test_browse_timeout_is_bounded_without_suggesting_resource_deletion(esxi, monkeypatch):
    monkeypatch.setattr(vmware.time, "monotonic", MagicMock(side_effect=[0, esxi.SOCKET_TIMEOUT]))
    with pytest.raises(VMwareError, match="retry browsing") as exc:
        esxi._wait_task(NS(info=NS(state="running")), "Browse datastore media", read_only=True)
    assert "clean up" not in str(exc.value)


@pytest.mark.parametrize("method,args", [("browse_iso_media", ()), ("download_iso", ("installer.iso", "unused.partial"))])
def test_media_read_operations_require_accessible_selected_datastore(esxi, method, args):
    esxi._host.datastore[0].summary.accessible = False
    kwargs = {"max_bytes": 100} if method == "download_iso" else {}
    with pytest.raises(VMwareError, match="inaccessible"):
        getattr(esxi, method)("ISO Datastore", *args, **kwargs)
    esxi._http.get.assert_not_called()
    assert esxi._browser_stub.calls == []


def test_download_uses_soap_cookie_verified_origin_and_streams_exclusive_file(esxi, tmp_path):
    remote = "Install Media/Édition/Installer image.ISO"
    destination = tmp_path / "new.partial"
    assert esxi.download_iso("ISO Datastore", remote, destination, max_bytes=100, expected_size=8) == 8
    assert destination.read_bytes() == b"installr"
    assert destination.stat().st_mode & 0o777 == 0o600
    args, kwargs = esxi._http.get.call_args
    assert args == ("https://esxi.example.test:443/folder/" + quote(remote, safe="/"),)
    assert kwargs["params"] == {"dcPath": "ha-datacenter", "dsName": "ISO Datastore"}
    assert kwargs["headers"] == {"Cookie": 'vmware_soap_session="private-session"', "Accept-Encoding": "identity"}
    assert "private-password" not in str(kwargs)
    assert kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert all(0 < value <= 120 for value in kwargs["timeout"])
    assert "data" not in kwargs and "auth" not in kwargs
    assert esxi._http.get.return_value.raw.read1.call_count == 4
    esxi._http.get.return_value.raw.read1.assert_called_with(1024**2, decode_content=False)
    esxi._http.get.return_value.close.assert_called_once()
    esxi._content.fileManager.MakeDirectory.assert_not_called()
    esxi._content.fileManager.DeleteDatastoreFile_Task.assert_not_called()


def test_download_reads_real_urllib3_stream_without_buffering_entire_body(esxi, tmp_path):
    data = b"ISO bytes" * 200000
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Length"] = str(len(data))
    response.raw = HTTPResponse(body=io.BytesIO(data), headers=response.headers, preload_content=False)
    esxi._http.get.return_value = response
    destination = tmp_path / "new.partial"
    assert esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=len(data)) == len(data)
    assert destination.read_bytes() == data
    assert response.raw.closed


@pytest.mark.parametrize("pinned", [False, True])
def test_download_preserves_ca_bundle_or_pinned_tls_context(esxi, tmp_path, monkeypatch, pinned):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/private/certificate-bundle.pem")
    if pinned:
        esxi._trusted_certificate = "already-validated-pem"
        esxi._tls_context = MagicMock()
    esxi.download_iso("ISO Datastore", "installer.iso", tmp_path / "new.partial", max_bytes=100)
    assert esxi._http.get.call_args.kwargs["verify"] == (True if pinned else "/private/certificate-bundle.pem")
    if pinned:
        esxi._tls_context.ensure_valid.assert_called_once()


def test_expired_pin_blocks_download_before_cookie_or_destination(esxi, tmp_path):
    esxi._tls_context = MagicMock()
    esxi._tls_context.ensure_valid.side_effect = ssl.SSLCertVerificationError("expired")
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="TLS certificate"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    esxi._http.get.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize("path", [
    "installer.vmdk", "installer.iso/extra", "", "/installer.iso", "../installer.iso", "isos/../installer.iso",
    "isos//installer.iso", "isos\\installer.iso", "%2e%2e/installer.iso", "%252e%252e/installer.iso",
    "[other] installer.iso", "bad\n.iso", "bad\x00.iso", None,
])
def test_download_rejects_unsafe_or_non_iso_paths(esxi, tmp_path, path):
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError):
        esxi.download_iso("ISO Datastore", path, destination, max_bytes=100)
    assert not destination.exists()
    esxi._http.get.assert_not_called()


@pytest.mark.parametrize("status,message", [(302, "redirect"), (307, "redirect"), (401, "denied"), (403, "denied"), (404, "not found"), (500, "HTTP 500")])
def test_download_http_failures_close_response_discard_partial_and_never_expose_body(esxi, tmp_path, status, message):
    response = esxi._http.get.return_value
    response.status_code = status
    response.text = "private-password private-session"
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match=message) as exc:
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert not destination.exists()
    assert "private-password" not in str(exc.value)
    assert "private-session" not in str(exc.value)
    response.raw.read1.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("length", ["bad", "-1", "1, 2", "0", "9999999999999999999999999999", "101"])
def test_download_rejects_invalid_empty_or_oversize_content_length(esxi, tmp_path, length):
    response = esxi._http.get.return_value
    response.headers = {"Content-Length": length}
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert not destination.exists()
    response.raw.read1.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("length,chunks,expected", [
    ("10", [b"short"], None),
    ("3", [b"too long"], None),
    (None, [b"short"], 8),
    (None, [b"too long for expected size"], 8),
    (None, [], None),
    ("9", [b"123456789"], 8),
])
def test_download_rejects_partial_or_changed_files(esxi, tmp_path, length, chunks, expected):
    response = esxi._http.get.return_value
    response.headers = {} if length is None else {"Content-Length": length}
    response.raw.read1.side_effect = [*chunks, b""]
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="incomplete|size changed"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100, expected_size=expected)
    assert not destination.exists()
    response.close.assert_called_once()


def test_download_can_stream_without_content_length_and_obeys_limit(esxi, tmp_path):
    response = esxi._http.get.return_value
    response.headers = {}
    destination = tmp_path / "new.partial"
    assert esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=8, expected_size=8) == 8
    response.raw.read1.side_effect = [b"12345", b"67890", b""]
    overflow = tmp_path / "overflow.partial"
    with pytest.raises(VMwareError, match="exceeds"):
        esxi.download_iso("ISO Datastore", "installer.iso", overflow, max_bytes=8)
    assert not overflow.exists()


@pytest.mark.parametrize("expected", [0, -1, True, 101])
def test_download_rejects_invalid_or_oversize_expected_size_before_request(esxi, tmp_path, expected):
    with pytest.raises(VMwareError):
        esxi.download_iso("ISO Datastore", "installer.iso", tmp_path / "new.partial", max_bytes=100, expected_size=expected)
    esxi._http.get.assert_not_called()


@pytest.mark.parametrize("symlink", [False, True])
def test_download_never_replaces_or_removes_existing_destination(esxi, tmp_path, symlink):
    target = tmp_path / "original"
    target.write_bytes(b"preserve")
    destination = tmp_path / "existing.partial"
    if symlink:
        destination.symlink_to(target)
    else:
        destination.write_bytes(b"preserve")
    with pytest.raises(VMwareError, match="already exists"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert destination.read_bytes() == b"preserve"
    assert target.read_bytes() == b"preserve"
    assert destination.is_symlink() == symlink
    esxi._http.get.assert_not_called()


def test_download_checks_disk_space_before_request(esxi, tmp_path, monkeypatch):
    monkeypatch.setattr(vmware.shutil, "disk_usage", lambda _: NS(free=esxi.MEDIA_FREE_SPACE_RESERVE))
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="free space"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100, expected_size=8)
    assert not destination.exists()
    esxi._http.get.assert_not_called()


def test_chunked_download_rechecks_remaining_free_space(esxi, tmp_path, monkeypatch):
    readings = iter([esxi.MEDIA_FREE_SPACE_RESERVE + 100, esxi.MEDIA_FREE_SPACE_RESERVE])
    monkeypatch.setattr(vmware.shutil, "disk_usage", lambda _: NS(free=next(readings)))
    esxi._http.get.return_value.headers = {}
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="ran out of space"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert not destination.exists()
    esxi._http.get.return_value.close.assert_called_once()


def test_download_stream_errors_are_sanitized_and_cleaned(esxi, tmp_path):
    response = esxi._http.get.return_value
    response.headers = {}
    response.raw.read1.side_effect = [b"first", requests.ConnectionError("private-password private-session")]
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="HTTPS connection") as exc:
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert "private-password" not in str(exc.value)
    assert "private-session" not in str(exc.value)
    assert not destination.exists()
    response.close.assert_called_once()


def test_download_does_not_accept_transformed_http_body(esxi, tmp_path):
    response = esxi._http.get.return_value
    response.headers = {"Content-Encoding": "gzip"}
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="encoded download"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert not destination.exists()
    response.raw.read1.assert_not_called()


def test_download_total_deadline_stops_slow_progress_and_cleans_up(esxi, tmp_path, monkeypatch):
    # Initial deadline; before and after the first successful read; then a
    # second small read takes us past the overall budget despite making progress.
    monkeypatch.setattr(vmware.time, "monotonic", MagicMock(side_effect=[0, 1, 2, 3599, 3601]))
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="timed out"):
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert not destination.exists()
    assert esxi._http.get.return_value.raw.read1.call_count == 2
    esxi._http.get.return_value.close.assert_called_once()


def test_download_socket_read_timeout_is_sanitized_and_cleaned(esxi, tmp_path):
    response = esxi._http.get.return_value
    response.raw.read1.side_effect = ReadTimeoutError(None, "/folder/private-session", "private-password")
    destination = tmp_path / "new.partial"
    with pytest.raises(VMwareError, match="timed out") as exc:
        esxi.download_iso("ISO Datastore", "installer.iso", destination, max_bytes=100)
    assert "private-password" not in str(exc.value)
    assert "private-session" not in str(exc.value)
    assert not destination.exists()
    response.close.assert_called_once()


def test_browse_and_download_reject_blocking_event_loop_use(esxi, tmp_path):
    async def run():
        with pytest.raises(VMwareError, match="worker thread"):
            esxi.browse_iso_media("ISO Datastore")
        with pytest.raises(VMwareError, match="worker thread"):
            esxi.download_iso("ISO Datastore", "installer.iso", tmp_path / "new.partial", max_bytes=100)

    asyncio.run(run())
