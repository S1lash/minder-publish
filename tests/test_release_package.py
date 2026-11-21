"""Public artifact guards use actual resources and synthetic unsafe additions."""

import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from minder_publish import __version__
from minder_publish.errors import PublishError
from minder_publish.server.distribution import Distribution

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publish_release_audit", ROOT / "scripts" / "check-release.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def wheel_files():
    files = {
        source.relative_to(ROOT / "src").as_posix(): source.read_bytes()
        for source in (ROOT / "src" / "minder_publish").rglob("*")
        if source.is_file()
        and "__pycache__" not in source.parts
        and source.suffix not in (".pyc", ".pyo")
    }
    prefix = f"minder_publish-{__version__}.dist-info/"
    files.update(
        {
            prefix + "METADATA": (
                f"Metadata-Version: 2.4\nName: minder-publish\nVersion: {__version__}\n"
                "Requires-Python: >=3.10\nLicense-Expression: MIT\nLicense-File: LICENSE\n"
            ).encode(),
            prefix
            + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            prefix + "RECORD": b"",
            prefix + "licenses/LICENSE": (ROOT / "LICENSE").read_bytes(),
            prefix
            + "entry_points.txt": b"[console_scripts]\nminder-publish = minder_publish.client.cli:main\nminder-publish-server = minder_publish.server.commands:main\nminder-publish-mcp = minder_publish.mcp.server:main\n",
        }
    )
    files.update(
        {
            f"minder_publish-{__version__}.data/data/share/minder-publish/skills/minder-publish/"
            + path.relative_to(
                ROOT / "skill/minder-publish"
            ).as_posix(): path.read_bytes()
            for path in (ROOT / "skill/minder-publish").rglob("*")
            if path.is_file()
        }
    )
    return files


def build_wheel(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_complete_portable_wheel_passes_resource_and_source_checks(
    tmp_path, wheel_files
):
    result = checker.audit(
        build_wheel(tmp_path / "package.whl", wheel_files), source_root=ROOT
    )
    assert result["passed"], result["findings"]


@pytest.mark.parametrize(
    "removed",
    [
        "minder_publish/server/web/fonts/commissioner-OFL.txt",
        "minder_publish/server/migrations/core/002_up.sql",
        "minder_publish/server/migrations/core/005_up.sql",
        "minder_publish/server/web/publication-link.js",
        f"minder_publish-{__version__}.data/data/share/minder-publish/skills/minder-publish/SKILL.md",
        f"minder_publish-{__version__}.data/data/share/minder-publish/skills/minder-publish/references/cli.md",
        f"minder_publish-{__version__}.data/data/share/minder-publish/skills/minder-publish/references/mcp.md",
    ],
)
def test_missing_fonts_sql_or_packaged_skill_blocks_release(
    tmp_path, wheel_files, removed
):
    del wheel_files[removed]
    result = checker.audit(build_wheel(tmp_path / "package.whl", wheel_files))
    assert not result["passed"]
    assert any(
        item["code"] in ("missing_required_file", "missing_skill")
        for item in result["findings"]
    )


def test_private_paths_credentials_and_legacy_files_report_locations_without_secret_values(
    tmp_path, wheel_files
):
    credential = "mp_" + "A" * 40
    private = "/" + "Users" + "/private-owner/project"
    wheel_files["minder_publish/leak.txt"] = f"{private}\n{credential}".encode()
    wheel_files["deploy/old-runtime.env"] = b"not public"
    result = checker.audit(build_wheel(tmp_path / "package.whl", wheel_files))
    codes = {item["code"] for item in result["findings"]}
    assert {"personal_path", "credential_signature", "outside_public_boundary"} <= codes
    assert credential not in str(result["findings"]) and private not in str(
        result["findings"]
    )


def test_stale_source_and_unsafe_archive_paths_block_release(tmp_path, wheel_files):
    wheel_files["minder_publish/client/cli.py"] = b"old build"
    wheel_files["../outside.py"] = b"x"
    result = checker.audit(
        build_wheel(tmp_path / "package.whl", wheel_files), source_root=ROOT
    )
    assert {"stale_or_unowned_artifact_file", "unsafe_archive_path"} <= {
        item["code"] for item in result["findings"]
    }


def test_source_archive_rejects_links_without_extracting_them(tmp_path):
    path = tmp_path / "package.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("package-0.2.0/escape")
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside"
        archive.addfile(member)
    result = checker.audit(path)
    assert any(item["code"] == "archive_link_or_special" for item in result["findings"])
    assert not (tmp_path / "escape").exists()


def test_source_archive_rejects_build_machine_metadata(tmp_path):
    path = tmp_path / "package.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo("package")
        directory.type = tarfile.DIRTYPE
        directory.uid, directory.uname = 1234, "example-builder"
        archive.addfile(directory)
        member = tarfile.TarInfo("package/README.md")
        member.gid, member.gname = 5678, "example-group"
        member.pax_headers = {"comment": "example-build-comment"}
        archive.addfile(member)

    result = checker.audit(path)
    assert {
        item["path"] for item in result["findings"] if item["code"] == "archive_owner_metadata"
    } == {
        "package", "package/README.md",
    }
    assert any(item["code"] == "archive_pax_metadata" for item in result["findings"])
    assert any(item["code"] == "archive_gzip_metadata" for item in result["findings"])
    assert "example-builder" not in str(result["findings"])
    assert "example-group" not in str(result["findings"])
    assert "example-build-comment" not in str(result["findings"])


def test_source_distribution_normalization_preserves_content_and_permissions(tmp_path):
    path = tmp_path / "package.tar.gz"
    content = b"Release file.\n"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("package/run.sh")
        member.uid, member.uname = 1234, "example-builder"
        member.gid, member.gname = 5678, "example-group"
        member.mode = 0o755
        member.mtime = 1234567.5
        member.pax_headers = {"comment": "example-build-comment"}
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))

    subprocess.run(
        [sys.executable, str(ROOT / "scripts/normalize-sdist.py"), str(path)], check=True,
    )
    with tarfile.open(path) as archive:
        member = archive.getmember("package/run.sh")
        assert archive.extractfile(member).read() == content
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "root", "root")
        assert not member.pax_headers
        assert member.mtime == 0 and member.mode == 0o755
    _, entries, problems = checker.read_archive(path)
    assert entries == {"run.sh": content}
    assert not problems


@pytest.mark.skipif(
    not shutil.which("git"), reason="Git is required for source exports"
)
def test_source_export_checks_exact_git_tree_without_using_working_files(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])

    git("init", "--quiet")
    (checkout / ".gitignore").write_bytes(b"dist/\n")
    (checkout / "README.md").write_bytes(b"Canonical README\n")
    git("add", ".gitignore", "README.md")
    tree = git("write-tree").decode().strip()
    (checkout / "README.md").write_bytes(b"Unstaged changes\n")
    archive_path = tmp_path / "source.tar.gz"
    archive_path.write_bytes(
        git("archive", "--format=tar.gz", "--prefix=source/", tree)
    )

    result = checker.audit(archive_path, source_root=checkout, git_tree=tree)
    assert result["kind"] == "git-source"
    assert all(item["code"] == "missing_required_file" for item in result["findings"])

    with tarfile.open(archive_path, "w:gz") as archive:
        for name, content in {
            "README.md": b"Unstaged changes\n",
            "CONTRIBUTING.md": b"Extra file\n",
        }.items():
            member = tarfile.TarInfo("source/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

    result = checker.audit(archive_path, source_root=checkout, git_tree=tree)
    assert {
        item["code"]: item["path"]
        for item in result["findings"]
        if "git_tree" in item["code"]
    } == {
        "changed_git_tree_file": "README.md",
        "missing_git_tree_file": ".gitignore",
        "unexpected_git_tree_file": "CONTRIBUTING.md",
    }


def test_compressed_archive_expansion_is_bounded_before_read(tmp_path, monkeypatch):
    path = build_wheel(
        tmp_path / "bomb.whl", {"minder_publish/unexpected.bin": b"x" * 2000}
    )
    monkeypatch.setattr(checker, "MAX_ARCHIVE_BYTES", 1024)
    result = checker.audit(path)
    assert any(
        item["code"] == "expanded_archive_too_large" for item in result["findings"]
    )


def test_full_wheel_is_refused_when_configured_for_anonymous_download(
    tmp_path, wheel_files
):
    full_wheel = build_wheel(
        tmp_path / f"minder_publish-{__version__}-py3-none-any.whl",
        wheel_files,
    )
    with pytest.raises(PublishError) as error:
        Distribution("https://publish.example", client_wheel=full_wheel)
    assert error.value.code == "invalid_client_release"
    result = checker.audit(full_wheel, client_wheel=True)
    assert {"invalid_client_wheel", "outside_public_boundary"} <= {
        item["code"] for item in result["findings"]
    }
