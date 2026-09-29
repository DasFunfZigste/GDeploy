import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from scripts import build_release as release


TAG = "v1.2.3"
DIGEST = "sha256:" + "a" * 64
COMMIT = "b" * 40


@pytest.fixture
def release_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for source_name, _ in release.DEPLOYMENT_FILES:
        path = root / source_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Fixture for {source_name}\n")
    (root / "gdeploy").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "gdeploy"\nversion = "1.2.3"\n')
    (root / "gdeploy" / "__init__.py").write_text(
        'raise RuntimeError("The release script must not import this module")\n__version__ = "1.2.3"\n'
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Future work\n\n"
        "## [1.2.3] - 2026-09-29\n\n### Added\n\n- Version-specific release packaging.\n\n"
        "## [0.1.0] - 2026-09-01\n\n- Historical release entry.\n"
    )
    (root / "compose.release.yaml").write_text(
        "services:\n  gdeploy:\n    image: ${GDEPLOY_IMAGE:-ghcr.io/dasfunfzigste/gdeploy:0.1.0}\n"
    )
    (root / "docs" / "INSTALL.md").write_text(
        "# Install GDeploy from a release\n\nThis guide installs **GDeploy 0.1.0**.\n\n"
        "## Fresh Ubuntu\n\nInstall Docker first.\n\n"
        "```sh\nGDEPLOY_VERSION=0.1.0\ndocker pull ghcr.io/dasfunfzigste/gdeploy:0.1.0\n"
        "# This is a shell comment, not a heading\n[code example](LAB_VALIDATION.md)\n```\n\n"
        "## Existing Docker\n\n[Lab checklist](LAB_VALIDATION.md#acceptance)\n"
        "[Tagged checklist](https://github.com/DasFunfZigste/GDeploy/blob/v0.1.0/docs/LAB_VALIDATION.md)\n"
        "[Project](../README.md)\n`[inline example](LAB_VALIDATION.md)`\n\n"
        "## Upgrades\n\nPreserve the existing configuration and data volume.\n"
    )
    (root / "docs" / "RELEASING.md").write_text("# Maintainer release process\n")
    (root / "media" / ".gitkeep").write_bytes(b"")
    return root


@pytest.fixture
def release_output(tmp_path):
    output = tmp_path / "artifacts"
    output.mkdir()
    (output / "gdeploy-1.2.3-linux-amd64.image.tar.gz").write_bytes(b"an already-exported image archive")
    return output


def test_validate_release_uses_ast_without_importing_application(release_source):
    info = release.validate_release(release_source, TAG)
    assert info.version == "1.2.3"
    assert info.tag == TAG
    assert info.date == "2026-09-29"
    assert info.changes == "### Added\n\n- Version-specific release packaging."


@pytest.mark.parametrize("tag", ["1.2.3", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2", "v1.2.3-rc1", "v1.2.3+build"])
def test_validate_rejects_invalid_release_tags(release_source, tag):
    with pytest.raises(release.ReleaseError, match="vX.Y.Z"):
        release.validate_release(release_source, tag)


@pytest.mark.parametrize("target", ["pyproject.toml", "gdeploy/__init__.py"])
def test_validate_requires_both_package_versions_to_match(release_source, target):
    path = release_source / target
    path.write_text(path.read_text().replace("1.2.3", "1.2.4"))
    with pytest.raises(release.ReleaseError, match="must match"):
        release.validate_release(release_source, TAG)


@pytest.mark.parametrize(
    "changelog,reason",
    [
        ("## [1.2.3]\n\n- Undated change\n", "dated"),
        ("## [1.2.3] - 2026-02-30\n\n- Invalid date\n", "date is invalid"),
        ("## [1.2.3] - 2026-09-29\n\n### Added\n", "must contain changes"),
        ("## [1.2.3] - 2026-09-29\n\n- TODO document change\n", "placeholders"),
        ("## [1.2.3] - 2026-09-29\n\n- Release detail tbd\n", "placeholders"),
        ("## [1.2.3] - 2026-09-29\n- One\n## [1.2.3] - 2026-09-30\n- Two\n", "exactly one"),
    ],
)
def test_validate_requires_finished_dated_changelog(release_source, changelog, reason):
    (release_source / "CHANGELOG.md").write_text(changelog)
    with pytest.raises(release.ReleaseError, match=reason):
        release.validate_release(release_source, TAG)


def test_build_bundle_is_allowlisted_and_excludes_secrets_and_real_media(release_source, release_output):
    secret_paths = [".env", "docker.env", "bootstrap-credentials.txt", "media/ubuntu.iso", "media/splunk.tgz", "data/state.db"]
    for name in secret_paths:
        path = release_source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("private deployment data must not be published")
    artifacts = release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)

    with tarfile.open(artifacts["gdeploy-1.2.3-deploy.tar.gz"], "r:gz") as archive:
        expected = {destination for _, destination in release.DEPLOYMENT_FILES} | {"docs/RELEASING.md", "release.json"}
        assert set(archive.getnames()) == expected
        assert not set(secret_paths) & set(archive.getnames())
        assert all(member.isfile() and member.mode == 0o644 for member in archive.getmembers())
        assert b"ghcr.io/dasfunfzigste/gdeploy:1.2.3" in archive.extractfile("compose.yaml").read()
        assert archive.extractfile("INSTALL.md").read() == archive.extractfile("docs/INSTALL.md").read()
        assert archive.extractfile("release.json").read() == artifacts["release.json"].read_bytes()

    metadata = json.loads(artifacts["release.json"].read_text())
    assert metadata == {
        "version": "1.2.3", "tag": TAG, "source_commit": COMMIT, "platform": "linux/amd64",
        "image": release.IMAGE + ":1.2.3", "image_digest": DIGEST,
        "image_by_digest": release.IMAGE + "@" + DIGEST, "repository": release.REPOSITORY,
    }
    sums = dict(line.split("  ", 1)[::-1] for line in artifacts["SHA256SUMS"].read_text().splitlines())
    assert set(sums) == {"gdeploy-1.2.3-deploy.tar.gz", "gdeploy-1.2.3-linux-amd64.image.tar.gz", "release.json", "INSTALL.md", "CHANGELOG.md"}
    for name, digest in sums.items():
        assert hashlib.sha256((release_output / name).read_bytes()).hexdigest() == digest


def test_future_release_notes_include_complete_pinned_installation_walkthrough(release_source, release_output):
    # Exercise the real user-facing walkthrough, including commands near its end.
    actual_guide = Path(__file__).resolve().parents[1] / "docs" / "INSTALL.md"
    (release_source / "docs" / "INSTALL.md").write_bytes(actual_guide.read_bytes())
    artifacts = release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    guide = artifacts["INSTALL.md"].read_text()
    notes = artifacts["RELEASE_NOTES.md"].read_text()
    assert "0.1.0" not in guide
    assert "0.1.0" not in notes
    assert "GDEPLOY_VERSION=1.2.3" in guide
    assert "ghcr.io/dasfunfzigste/gdeploy:1.2.3" in notes
    assert release.IMAGE + "@" + DIGEST in notes
    assert release.REPOSITORY + "/commit/" + COMMIT in notes
    assert "Version-specific release packaging." in notes
    assert "Historical release entry." not in notes
    assert "Future work" not in notes
    assert release.installation_for_release_notes(guide, TAG).strip() in notes
    for heading in ["Install Docker on a new Ubuntu server", "docker run", "Upgrade"]:
        assert heading.lower() in notes.lower()
    assert f"{release.REPOSITORY}/blob/{TAG}/docs/LAB_VALIDATION.md" in notes
    for name in ["gdeploy-1.2.3-deploy.tar.gz", "gdeploy-1.2.3-linux-amd64.image.tar.gz", "SHA256SUMS"]:
        assert f"{release.REPOSITORY}/releases/download/{TAG}/{name}" in notes


def test_notes_rewrite_doc_links_and_headings_without_changing_code(release_source, release_output):
    artifacts = release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    notes = artifacts["RELEASE_NOTES.md"].read_text()
    assert "## Install GDeploy from a release" in notes
    assert "### Fresh Ubuntu" in notes
    assert f"[Lab checklist]({release.REPOSITORY}/blob/{TAG}/docs/LAB_VALIDATION.md#acceptance)" in notes
    assert f"[Project]({release.REPOSITORY}/blob/{TAG}/README.md)" in notes
    assert "# This is a shell comment, not a heading\n[code example](LAB_VALIDATION.md)" in notes
    assert "`[inline example](LAB_VALIDATION.md)`" in notes
    assert "\n## Upgrades\n" not in notes
    assert "### Upgrades" in notes
    assert "Preserve the existing configuration and data volume." in notes


def test_markdown_fences_and_indented_code_are_preserved():
    guide = "# Guide\n\n~~~~text\n## literal\n[link](README.md)\n~~~\n~~~~\n\n    [code](README.md)\n"
    rendered = release.installation_for_release_notes(guide, TAG)
    assert rendered == "#" + guide


def test_bundle_is_reproducible_and_releasing_guide_is_optional(release_source, release_output):
    (release_source / "docs" / "RELEASING.md").unlink()
    first = release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    bundle = first["gdeploy-1.2.3-deploy.tar.gz"].read_bytes()
    second = release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    assert bundle == second["gdeploy-1.2.3-deploy.tar.gz"].read_bytes()
    with tarfile.open(second["gdeploy-1.2.3-deploy.tar.gz"], "r:gz") as archive:
        assert "docs/RELEASING.md" not in archive.getnames()


@pytest.mark.parametrize("source_name", [".env.example", "docs/RELEASING.md", "gdeploy/__init__.py"])
def test_release_rejects_symlink_file_inputs(release_source, release_output, source_name):
    path = release_source / source_name
    existing = path.with_name(path.name + ".real")
    path.rename(existing)
    path.symlink_to(existing)
    with pytest.raises(release.ReleaseError, match="Symlink inputs"):
        release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    assert not (release_output / "release.json").exists()


def test_release_rejects_symlink_directory_inputs(release_source, release_output):
    docs = release_source / "docs"
    actual = release_source / "actual-docs"
    docs.rename(actual)
    docs.symlink_to(actual, target_is_directory=True)
    with pytest.raises(release.ReleaseError, match="Symlink inputs"):
        release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)


@pytest.mark.parametrize("state", ["missing", "empty", "symlink"])
def test_release_requires_existing_nonempty_regular_image_archive(release_source, release_output, state):
    image = release_output / "gdeploy-1.2.3-linux-amd64.image.tar.gz"
    if state == "empty":
        image.write_bytes(b"")
    else:
        image.unlink()
        if state == "symlink":
            image.symlink_to(release_source / "README.md")
    with pytest.raises(release.ReleaseError):
        release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    assert not (release_output / "release.json").exists()


@pytest.mark.parametrize("digest,commit", [("sha256:short", COMMIT), (DIGEST, "short"), ("z" * 64, COMMIT)])
def test_release_rejects_incomplete_provenance(release_source, release_output, digest, commit):
    with pytest.raises(release.ReleaseError):
        release.build_release(release_source, TAG, digest, commit, release_output)
    assert not (release_output / "release.json").exists()


def test_release_refuses_to_overwrite_symlink_output(release_source, release_output):
    target = release_output / "release.json"
    target.symlink_to(release_source / "README.md")
    original = (release_source / "README.md").read_bytes()
    with pytest.raises(release.ReleaseError, match="Symlink output"):
        release.build_release(release_source, TAG, DIGEST, COMMIT, release_output)
    assert (release_source / "README.md").read_bytes() == original


def test_validate_cli_prints_only_the_version(release_source, monkeypatch, capsys):
    monkeypatch.setattr(release, "__file__", str(release_source / "scripts" / "build_release.py"))
    release.main(["--tag", TAG, "--validate-only"])
    assert capsys.readouterr().out == "1.2.3\n"


def test_cli_requires_packaging_arguments(release_source, monkeypatch, capsys):
    monkeypatch.setattr(release, "__file__", str(release_source / "scripts" / "build_release.py"))
    with pytest.raises(SystemExit) as error:
        release.main(["--tag", TAG])
    assert error.value.code == 2
    assert "--image-digest, --commit, and --output are required" in capsys.readouterr().err
