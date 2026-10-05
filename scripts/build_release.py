#!/usr/bin/env python3
"""Validate a release and build its deployment bundle, checksums, and release page."""

import argparse
import ast
import datetime
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


REPOSITORY = "https://github.com/DasFunfZigste/GDeploy"
IMAGE = "ghcr.io/dasfunfzigste/gdeploy"
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
DEPLOYMENT_FILES = (
    ("compose.release.yaml", "compose.yaml"),
    ("scripts/configure.py", "scripts/configure.py"),
    (".env.example", ".env.example"),
    ("README.md", "README.md"),
    ("CHANGELOG.md", "CHANGELOG.md"),
    ("docs/INSTALL.md", "INSTALL.md"),
    ("docs/INSTALL.md", "docs/INSTALL.md"),
    ("docs/LAB_VALIDATION.md", "docs/LAB_VALIDATION.md"),
    ("docs/FLEETMANAGER.md", "docs/FLEETMANAGER.md"),
    ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.md"),
    ("docs/overview.png", "docs/overview.png"),
    ("media/.gitkeep", "media/.gitkeep"),
)


class ReleaseError(ValueError):
    """A release input is missing, unsafe, or inconsistent."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    date: str
    changes: str


def regular_input(root: Path, relative: str, *, optional: bool = False) -> Path | None:
    """Require an ordinary file, checking both the file and its parent directories."""
    candidate = root
    if candidate.is_symlink():
        raise ReleaseError(f"Symlink inputs are not allowed: {root}")
    for part in Path(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ReleaseError(f"Symlink inputs are not allowed: {candidate}")
    if optional and not candidate.exists():
        return None
    if not candidate.is_file():
        raise ReleaseError(f"Required release input is not a regular file: {candidate}")
    return candidate


def _package_version(path: Path) -> str:
    """Inspect the assignment without importing the application or its dependencies."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
                assignments.append(ast.literal_eval(node.value))
        if len(assignments) != 1 or not isinstance(assignments[0], str):
            raise ValueError("expected one literal string assignment")
        return assignments[0]
    except (SyntaxError, ValueError, TypeError) as error:
        raise ReleaseError("gdeploy/__init__.py must assign __version__ to one literal string.") from error


def validate_release(root: Path, tag: str) -> ReleaseInfo:
    if not re.fullmatch("v" + VERSION_PATTERN, tag):
        raise ReleaseError("Release tag must be vX.Y.Z with no leading zeros or prerelease suffix.")
    version = tag[1:]
    project_file = regular_input(root, "pyproject.toml")
    package_file = regular_input(root, "gdeploy/__init__.py")
    changelog_file = regular_input(root, "CHANGELOG.md")
    try:
        project_version = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError("pyproject.toml must define project.version.") from error
    package_version = _package_version(package_file)
    if project_version != version or package_version != version:
        raise ReleaseError(
            f"Tag {tag} must match pyproject.toml ({project_version}) and gdeploy/__init__.py ({package_version})."
        )
    changelog = changelog_file.read_text(encoding="utf-8")
    headers = list(re.finditer(r"(?m)^##[ \t]+\[" + re.escape(version) + r"\][^\n]*$", changelog))
    if len(headers) != 1:
        raise ReleaseError(f"CHANGELOG.md must contain exactly one dated ## [{version}] - YYYY-MM-DD section.")
    header = headers[0]
    date_match = re.fullmatch(r"## \[" + re.escape(version) + r"\] - (\d{4}-\d{2}-\d{2})[ \t]*", header.group())
    if not date_match:
        raise ReleaseError(f"CHANGELOG.md needs a dated ## [{version}] - YYYY-MM-DD heading.")
    date = date_match.group(1)
    try:
        datetime.date.fromisoformat(date)
    except ValueError as error:
        raise ReleaseError("The changelog release date is invalid.") from error
    remaining = changelog[header.end():]
    next_section = re.search(r"(?m)^##(?:[ \t]|$)", remaining)
    changes = remaining[:next_section.start() if next_section else len(remaining)].strip()
    if not re.sub(r"(?m)^#{1,6}[ \t].*$", "", changes).strip():
        raise ReleaseError("The changelog release section must contain changes, not just headings.")
    if re.search(r"\b(?:TODO|TBD)\b", changes, re.IGNORECASE):
        raise ReleaseError("The changelog release section still contains TODO or TBD placeholders.")
    return ReleaseInfo(version, tag, date, changes)


def render_version(text: str, version: str) -> str:
    """Pin command examples, Compose defaults, and the guide's version to this release."""
    text = re.sub(r"\bGDEPLOY_VERSION=" + VERSION_PATTERN + r"(?![0-9A-Za-z_.-])", "GDEPLOY_VERSION=" + version, text)
    text = re.sub(re.escape(IMAGE) + ":" + VERSION_PATTERN + r"(?![0-9A-Za-z_.-])", IMAGE + ":" + version, text)
    text = re.sub(r"\bGDeploy " + VERSION_PATTERN + r"(?![0-9A-Za-z_.-])", "GDeploy " + version, text)
    return re.sub(
        re.escape(REPOSITORY) + r"/(blob|tree|releases/tag|releases/download)/v" + VERSION_PATTERN + r"(?=/|[\s)#]|$)",
        lambda match: REPOSITORY + "/" + match.group(1) + "/v" + version,
        text,
    )


def _relative_link(target: str, tag: str, source_directory: str) -> str:
    wrapped = target.startswith("<") and target.endswith(">")
    url = target[1:-1] if wrapped else target
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return target
    path = posixpath.normpath(posixpath.join(source_directory, parts.path))
    if path == ".." or path.startswith("../"):
        raise ReleaseError(f"Documentation link escapes the repository: {url}")
    absolute = urlunsplit(("https", "github.com", f"/DasFunfZigste/GDeploy/blob/{tag}/{path}", parts.query, parts.fragment))
    return f"<{absolute}>" if wrapped else absolute


def _rewrite_prose_links(line: str, tag: str, source_directory: str) -> str:
    pattern = r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|[^\s)]+)((?:[ \t]+[\"'][^\n]*[\"'])?\))"
    return re.sub(
        pattern,
        lambda match: match.group(1) + _relative_link(match.group(2), tag, source_directory) + match.group(3),
        line,
    )


def _outside_inline_code(line: str, transform) -> str:
    result = []
    position = 0
    while opening := re.search(r"`+", line[position:]):
        start = position + opening.start()
        content_start = position + opening.end()
        closing = re.search(r"(?<!`)" + re.escape(opening.group()) + r"(?!`)", line[content_start:])
        if closing is None:
            break
        end = content_start + closing.end()
        result.extend((transform(line[position:start]), line[start:end]))
        position = end
    result.append(transform(line[position:]))
    return "".join(result)


def installation_for_release_notes(guide: str, tag: str) -> str:
    """Nest guide headings and resolve doc links while leaving code content intact."""
    lines = []
    fence_character = None
    fence_length = 0
    for line in guide.splitlines(keepends=True):
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence_character:
            lines.append(line)
            if fence and fence.group(1)[0] == fence_character and len(fence.group(1)) >= fence_length:
                if not line[fence.end():].strip():
                    fence_character = None
            continue
        if fence:
            fence_character = fence.group(1)[0]
            fence_length = len(fence.group(1))
            lines.append(line)
            continue
        line = re.sub(r"^(#{1,5})([ \t]+)", r"#\1\2", line)
        # Indented code is also kept literal, including documentation link examples.
        if not line.startswith(("    ", "\t")):
            line = _outside_inline_code(line, lambda prose: _rewrite_prose_links(prose, tag, "docs"))
        lines.append(line)
    return "".join(lines)


def release_notes(info: ReleaseInfo, metadata: dict, guide: str, asset_names: list[str]) -> str:
    downloads = "\n".join(
        f"- [{name}]({REPOSITORY}/releases/download/{info.tag}/{quote(name)})" for name in asset_names
    )
    return (
        f"# GDeploy {info.tag}\n\n"
        f"Released {info.date}.\n\n"
        f"| Release detail | Value |\n| --- | --- |\n"
        f"| Version | `{info.version}` |\n"
        f"| Docker image | `{metadata['image']}` |\n"
        f"| Registry digest | `{metadata['image_digest']}` |\n"
        f"| Image pinned by digest | `{metadata['image_by_digest']}` |\n"
        "| Docker host platform | `linux/amd64` |\n"
        f"| Source | [{metadata['source_commit'][:12]}]({REPOSITORY}/commit/{metadata['source_commit']}) |\n\n"
        "The repository and image are private; download and registry authentication steps are included below. "
        "The Docker image archive provides an alternative to a registry pull.\n\n"
        f"## Changelog\n\n{info.changes}\n\n"
        f"## Download assets\n\n{downloads}\n\n"
        "Verify downloaded files against `SHA256SUMS` before using them. "
        "The deployment bundle contains configuration templates and documentation; "
        "Operating-system media, licensed software installers and product licenses are supplied separately.\n\n"
        + installation_for_release_notes(guide, info.tag).rstrip()
        + "\n"
    )


def _write_artifact(path: Path, content: bytes):
    if path.is_symlink():
        raise ReleaseError(f"Symlink output files are not allowed: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _deployment_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, content in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                entry.mode = 0o644
                entry.mtime = 0
                archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def build_release(root: Path, tag: str, image_digest: str, commit: str, output: Path) -> dict[str, Path]:
    info = validate_release(root, tag)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ReleaseError("Image digest must be sha256: followed by 64 lowercase hexadecimal characters.")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("Source commit must be a full 40-character lowercase Git SHA.")
    image_name = f"gdeploy-{info.version}-linux-amd64.image.tar.gz"
    image_archive = regular_input(output, image_name)
    if image_archive.stat().st_size == 0:
        raise ReleaseError("The Docker image archive must be produced before packaging and must not be empty.")

    files = {}
    for source_name, archive_name in DEPLOYMENT_FILES:
        source = regular_input(root, source_name)
        content = source.read_bytes()
        if source.suffix in {".md", ".yaml"} and source_name != "CHANGELOG.md":
            content = render_version(content.decode("utf-8"), info.version).encode("utf-8")
        files[archive_name] = content
    releasing = regular_input(root, "docs/RELEASING.md", optional=True)
    if releasing:
        files["docs/RELEASING.md"] = render_version(releasing.read_text(encoding="utf-8"), info.version).encode("utf-8")
    metadata = {
        "version": info.version,
        "tag": info.tag,
        "source_commit": commit,
        "platform": "linux/amd64",
        "image": f"{IMAGE}:{info.version}",
        "image_digest": image_digest,
        "image_by_digest": f"{IMAGE}@{image_digest}",
        "repository": REPOSITORY,
    }
    metadata_bytes = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files["release.json"] = metadata_bytes
    bundle_name = f"gdeploy-{info.version}-deploy.tar.gz"
    checksummed_names = [bundle_name, image_name, "release.json", "INSTALL.md", "CHANGELOG.md"]
    notes = release_notes(info, metadata, files["INSTALL.md"].decode("utf-8"), checksummed_names + ["SHA256SUMS"])
    artifacts = {
        bundle_name: _deployment_archive(files),
        "release.json": metadata_bytes,
        "INSTALL.md": files["INSTALL.md"],
        "CHANGELOG.md": files["CHANGELOG.md"],
        "RELEASE_NOTES.md": notes.encode("utf-8"),
    }
    for name in [*artifacts, "SHA256SUMS"]:
        if (output / name).is_symlink():
            raise ReleaseError(f"Symlink output files are not allowed: {output / name}")
    for name, content in artifacts.items():
        _write_artifact(output / name, content)
    sums = []
    for name in sorted(checksummed_names):
        with (output / name).open("rb") as stream:
            sums.append(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {name}\n")
    _write_artifact(output / "SHA256SUMS", "".join(sums).encode("utf-8"))
    return {name: output / name for name in [*artifacts, image_name, "SHA256SUMS"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Stable release tag, for example v0.1.0.")
    parser.add_argument("--validate-only", action="store_true", help="Validate version/changelog inputs and print the version.")
    parser.add_argument("--image-digest", help="Published image digest, sha256: followed by 64 hex characters.")
    parser.add_argument("--commit", help="Full 40-character source commit SHA.")
    parser.add_argument("--output", type=Path, help="Artifact directory containing the previously exported Docker image archive.")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if arguments.validate_only:
            print(validate_release(root, arguments.tag).version)
            return
        if not all((arguments.image_digest, arguments.commit, arguments.output)):
            parser.error("--image-digest, --commit, and --output are required unless --validate-only is used")
        paths = build_release(root, arguments.tag, arguments.image_digest, arguments.commit, arguments.output.absolute())
    except (ReleaseError, OSError, UnicodeError) as error:
        parser.error(str(error))
    for path in paths.values():
        print(path)


if __name__ == "__main__":
    main()
