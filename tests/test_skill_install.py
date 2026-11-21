"""Managed skill installs keep references complete before publishing the entry."""

import os
from pathlib import Path

import pytest

from minder_publish.client import skills
from minder_publish.client.errors import ClientError


@pytest.fixture
def skill_source(tmp_path):
    root = tmp_path / "source"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: minder-publish\ndescription: Publish results.\n---\n"
        + skills.MARKER
        + "\nRead [CLI](references/cli.md).\n"
    )
    (root / "references" / "cli.md").write_text("Original instructions.\n")
    return root


def test_recursive_install_publishes_complete_immutable_references(
    skill_source, tmp_path
):
    home = tmp_path / "home"
    plan = skills.install(skill_source / "SKILL.md", home=home, runtimes=["codex"])
    assert plan[0]["changed"] and not plan[0]["applied"]
    assert not home.exists()
    installed = skills.install(skill_source, home=home, runtimes=["codex", "claude"], apply=True)
    assert installed[0]["generation"] != installed[1]["generation"]
    entries = [
        home / name / "skills" / "minder-publish" / "SKILL.md"
        for name in (".agents", ".claude")
    ]
    for entry, name in zip(entries, ("minder-publish", "minder:publish")):
        text = entry.read_text()
        assert f"\nname: {name}\n" in text
        link = text.split("](", 1)[1].split(")", 1)[0]
        assert (entry.parent / link).read_text() == "Original instructions.\n"
    assert not any(
        p["changed"]
        for p in skills.install(
            skill_source, home=home, runtimes=["codex", "claude"], apply=True
        )
    )


def test_claude_runtime_preserves_windows_line_endings():
    files = {"SKILL.md": b"---\r\nname: minder-publish\r\n---\r\n"}
    rendered = skills.runtime_skill_files(files, "claude")
    assert rendered["SKILL.md"] == b"---\r\nname: minder:publish\r\n---\r\n"
    assert skills.runtime_skill_files(files, "codex") == files


def test_update_retains_old_references_and_unmanaged_files(skill_source, tmp_path):
    home = tmp_path / "home"
    skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    entry = home / ".agents" / "skills" / "minder-publish" / "SKILL.md"
    old_link = entry.read_text().split("](", 1)[1].split(")", 1)[0]
    local = entry.parent / "notes.txt"
    local.write_text("User notes.")
    (skill_source / "references" / "cli.md").write_text("New instructions.\n")
    skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    new_link = entry.read_text().split("](", 1)[1].split(")", 1)[0]
    assert old_link != new_link
    assert (entry.parent / old_link).read_text() == "Original instructions.\n"
    assert (entry.parent / new_link).read_text() == "New instructions.\n"
    assert local.read_text() == "User notes."


def test_failed_entry_publish_leaves_old_skill_and_references_working(
    skill_source, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    entry = home / ".agents" / "skills" / "minder-publish" / "SKILL.md"
    old = entry.read_bytes()
    (skill_source / "references" / "cli.md").write_text("Changed instructions.\n")
    replace = os.replace

    def fail_entry(source, target):
        if Path(target) == entry:
            raise OSError("Synthetic entry publication failure")
        return replace(source, target)

    monkeypatch.setattr(skills.os, "replace", fail_entry)
    with pytest.raises(OSError, match="Synthetic"):
        skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    assert entry.read_bytes() == old
    old_link = old.decode().split("](", 1)[1].split(")", 1)[0]
    assert (entry.parent / old_link).read_text() == "Original instructions.\n"


def test_unmanaged_entry_is_never_overwritten(skill_source, tmp_path):
    target = tmp_path / "home" / ".agents" / "skills" / "minder-publish" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("User-owned skill.")
    with pytest.raises(ClientError) as error:
        skills.install(
            skill_source, home=tmp_path / "home", runtimes=["codex"], apply=True
        )
    assert error.value.code == "unmanaged_skill_target"
    assert target.read_text() == "User-owned skill."


@pytest.mark.skipif(
    os.name == "nt", reason="Symlink creation requires Windows privileges"
)
def test_symlink_source_or_runtime_parent_is_rejected(skill_source, tmp_path):
    (skill_source / "references" / "outside.md").symlink_to(skill_source / "SKILL.md")
    with pytest.raises(ClientError):
        skills.install(skill_source, home=tmp_path / "home", runtimes=["codex"])
    (skill_source / "references" / "outside.md").unlink()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agents").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ClientError):
        skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    assert not (tmp_path / "elsewhere").exists()


def test_skill_snapshot_is_bounded(skill_source, tmp_path):
    (skill_source / "references" / "large.txt").write_bytes(
        b"x" * (skills.MAX_FILE_BYTES + 1)
    )
    with pytest.raises(ClientError) as error:
        skills.install(skill_source, home=tmp_path / "home", runtimes=["codex"])
    assert error.value.code == "skill_size_limit"


def test_modified_immutable_generation_is_not_reused(skill_source, tmp_path):
    home = tmp_path / "home"
    skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    entry = home / ".agents" / "skills" / "minder-publish" / "SKILL.md"
    link = entry.read_text().split("](", 1)[1].split(")", 1)[0]
    (entry.parent / link).write_text("Locally modified cached instructions.")
    with pytest.raises(ClientError) as error:
        skills.install(skill_source, home=home, runtimes=["codex"], apply=True)
    assert error.value.code == "modified_skill_generation"


def test_managed_legacy_codex_migration_is_reviewable_idempotent_and_preserves_user_files(skill_source, tmp_path):
    home = tmp_path / 'home'
    legacy = home / '.codex/skills/minder-publish/SKILL.md'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('---\nname: minder-publish\n---\n' + skills.MARKER + '\nold')
    note = legacy.parent / 'notes.txt'
    note.write_text('owner note')
    plan = skills.install(skill_source, home=home, runtimes=['codex'])[0]
    assert plan['migrates_from'] == str(legacy) and plan['changed'] and legacy.exists()
    assert not (home / '.agents').exists()
    result = skills.install(skill_source, home=home, runtimes=['codex'], apply=True)[0]
    assert result['applied'] and not legacy.exists() and note.read_text() == 'owner note'
    assert (home / '.agents/skills/minder-publish/SKILL.md').exists()
    assert not skills.install(skill_source, home=home, runtimes=['codex'], apply=True)[0]['changed']


def test_unmanaged_legacy_entry_blocks_before_new_install(skill_source, tmp_path):
    home = tmp_path / 'home'
    legacy = home / '.codex/skills/minder-publish/SKILL.md'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('owner-managed')
    with pytest.raises(ClientError):
        skills.install(skill_source, home=home, runtimes=['codex'], apply=True)
    assert legacy.read_text() == 'owner-managed' and not (home / '.agents').exists()


def test_legacy_removal_failure_rolls_back_new_entry(skill_source, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    legacy = home / '.codex/skills/minder-publish/SKILL.md'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(skills.MARKER)
    unlink = Path.unlink
    def fail_legacy(path, *args, **kwargs):
        if path == legacy:
            raise OSError('synthetic removal failure')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_legacy)
    with pytest.raises(OSError, match='synthetic removal failure'):
        skills.install(skill_source, home=home, runtimes=['codex'], apply=True)
    assert legacy.read_text() == skills.MARKER
    assert not (home / '.agents/skills/minder-publish/SKILL.md').exists()


def test_real_skill_reference_contains_quoted_executable_path_with_spaces(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    executable = tmp_path / 'path with spaces' / 'minder-publish'
    monkeypatch.setattr(skills, 'executable_path', lambda: executable)
    plans = skills.install(skills.canonical_source(), home=home, runtimes=['codex', 'claude'], apply=True)
    import re
    for plan in plans:
        entry = Path(plan['path'])
        text = entry.read_text()
        references = re.findall(r'\]\(([^)]+)\)', text)
        assert references
        for reference in references:
            assert (entry.parent / reference).is_file(), reference
        installation = next(entry.parent / reference for reference in references if reference.endswith('/installation.md'))
        assert str(executable) in installation.read_text()
