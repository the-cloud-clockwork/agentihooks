import dataclasses
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.swarm_v2.artifacts.base import ArtifactRef
from scripts.swarm_v2.checkpoints import git_capture, model
from tests import sv2_ckp02_cases as cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-CKP-02"
DESCRIBED = [
    "untracked files git ignores",
    "cache directories: .mypy_cache, .pytest_cache, .ruff_cache, .tox, .venv, __pycache__, node_modules",
    "compiled files: .pyc, .pyo",
    "credential files by name",
    "untracked files whose content matches a secret pattern",
    "links and other files that are not regular",
    "untracked files over 1048576 bytes",
    "untracked files past 16777216 bytes in total",
]


def _pushed(root: Path) -> Path:
    remote, work = root / "remote.git", root / "worker"
    cases.git(root, "init", "-q", "--bare", str(remote))
    cases.git(root, "init", "-q", str(work))
    cases.git(work, "checkout", "-q", "-b", cases.BRANCH)
    cases.write(work / "app.py", "print('base')\n")
    cases.git(work, "add", "app.py")
    cases.git(work, "commit", "-q", "-m", "base")
    cases.git(work, "remote", "add", "origin", str(remote))
    cases.git(work, "push", "-q", "origin", "HEAD:refs/heads/dev")
    cases.git(work, "fetch", "-q", "origin")
    return work


def _names(source: git_capture.SourceCapture) -> list[str]:
    return cases.members(source)


def _round_trip(root: Path, source: git_capture.SourceCapture) -> Path:
    validator = model.Validator(cases.store(root / "durable"))
    assert cases.commit(validator, source, "ckpt-src-0001").authoritative
    target = cases.clone(root, "restored")
    cases.git(target, "branch", cases.BRANCH)
    reader = model.Validator(cases.store(root / "durable", execution="e2", generation=3))
    assert git_capture.restore(reader, "ckpt-src-0001", target).authoritative
    assert cases.git(target, "symbolic-ref", "--short", "HEAD").strip() == cases.BRANCH
    return target


def test_policy_defaults_and_description():
    assert git_capture.Policy() == git_capture.Policy(1048576, 16777216)
    assert git_capture.Policy().describe() == DESCRIBED
    assert git_capture.Policy(5, 9).describe()[-2:] == [
        "untracked files over 5 bytes",
        "untracked files past 9 bytes in total",
    ]


def test_a_pushed_head_carries_no_bundle_and_restores_untracked_only(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "draft.md", "draft\n")
    source = git_capture.capture(work, "origin/dev")
    assert source.branch_state == git_capture.PUSHED
    assert source.base_commit == source.head_commit
    assert source.branch_name == cases.BRANCH
    assert sorted(source.materials) == [git_capture.STAGED, git_capture.UNSTAGED, git_capture.UNTRACKED]
    assert source.materials[git_capture.STAGED] == b""
    assert source.materials[git_capture.UNSTAGED] == b""
    target = _round_trip(tmp_path, source)
    assert (target / "draft.md").read_text() == "draft\n"
    assert cases.state(target)["tracked_status"] == []


def test_fields_and_metrics_describe_every_material(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "app.py", "print('changed')\n")
    source = git_capture.capture(work, "origin/dev", git_capture.Policy(10, 20))
    fields = source.fields()
    refs = {role: ArtifactRef.of(data) for role, data in source.materials.items()}
    assert fields == {
        "base_commit": source.base_commit,
        "head_commit": source.head_commit,
        "branch_name": cases.BRANCH,
        "branch_state": "pushed",
        "inventory": {role: {"sha256": ref.sha256, "size": ref.size} for role, ref in refs.items()},
        "artifact_hashes": {role: "sha256:" + ref.sha256 for role, ref in refs.items()},
        "exclusions": git_capture.Policy(10, 20).describe(),
    }
    assert source.metrics() == {"checkpoint_source_bytes": {role: ref.size for role, ref in refs.items()}}
    assert source.metrics()[git_capture.METRIC][git_capture.UNSTAGED] > 0


def test_merged_keeps_the_manifest_materials_and_exclusions(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    manifest = {
        "checkpoint_id": "c1",
        "base_commit": "a" * 40,
        "inventory": {"normalized_transcript": {"sha256": "b" * 64, "size": 3}},
        "artifact_hashes": {"normalized_transcript": "sha256:" + "b" * 64},
        "exclusions": ["environment variables"],
    }
    doc = git_capture.merged(manifest, source)
    fields = source.fields()
    assert doc["checkpoint_id"] == "c1"
    assert doc["base_commit"] == source.base_commit
    assert doc["inventory"] == {
        "normalized_transcript": manifest["inventory"]["normalized_transcript"],
        **fields["inventory"],
    }
    assert doc["artifact_hashes"] == {"normalized_transcript": "sha256:" + "b" * 64, **fields["artifact_hashes"]}
    assert doc["exclusions"] == ["environment variables", *fields["exclusions"]]
    assert git_capture.merged({}, source) == fields


def test_merged_drops_source_materials_the_manifest_carried_from_an_earlier_capture(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    stale = {"sha256": "c" * 64, "size": 9}
    roles = [*git_capture.SOURCE_ROLES, "source_bundle"]
    manifest = {
        "branch_state": "bundled",
        "inventory": {"normalized_transcript": {"sha256": "b" * 64, "size": 3}, **dict.fromkeys(roles, stale)},
        "artifact_hashes": {
            "normalized_transcript": "sha256:" + "b" * 64,
            **dict.fromkeys(roles, "sha256:" + "c" * 64),
        },
    }
    doc = git_capture.merged(manifest, source)
    fields = source.fields()
    assert doc["branch_state"] == "pushed"
    assert doc["inventory"] == {"normalized_transcript": {"sha256": "b" * 64, "size": 3}, **fields["inventory"]}
    assert doc["artifact_hashes"] == {"normalized_transcript": "sha256:" + "b" * 64, **fields["artifact_hashes"]}
    assert "source_bundle" not in doc["inventory"]


def test_caches_credentials_links_and_secrets_are_counted_not_archived(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "__pycache__" / "app.cpython-312.pyc", "compiled\n")
    cases.write(work / "pkg" / "node_modules" / "x.js", "cached\n")
    cases.write(work / "loose.pyc", "compiled\n")
    cases.write(work / "loose.pyo", "compiled\n")
    cases.write(work / "deploy" / ".netrc", "machine fixture\n")
    cases.write(work / "token.txt", "slack " + "xox" + "b-" + "0123456789abcdef\n")
    cases.write(work / "keep.md", "keep\n")
    (work / "link.md").symlink_to(work / "keep.md")
    source = git_capture.capture(work, "origin/dev")
    assert _names(source) == ["keep.md"]
    assert source.exclusions[len(DESCRIBED) :] == (
        "excluded cache: 4",
        "excluded credential: 1",
        "excluded link: 1",
        "excluded secret: 1",
    )


def test_ignored_files_are_not_listed_and_binary_content_is_kept(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / ".gitignore", "*.log\n")
    cases.git(work, "add", ".gitignore")
    cases.git(work, "commit", "-q", "-m", "ignore logs")
    cases.write(work / "run.log", "ignored\n")
    (work / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    cases.write(work / "b.md", "b\n")
    cases.write(work / "a.md", "a\n")
    source = git_capture.capture(work, "origin/dev")
    assert _names(source) == ["a.md", "b.md", "blob.bin"]
    assert source.exclusions == tuple(DESCRIBED)
    target = _round_trip(tmp_path, source)
    assert (target / "blob.bin").read_bytes() == b"\xff\xfe\x00binary"
    assert not (target / "run.log").exists()


def test_file_and_total_limits_are_inclusive(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "a.md", "12345")
    cases.write(work / "b.md", "1234")
    cases.write(work / "c.md", "123456")
    cases.write(work / "d.md", "1")
    kept = git_capture.capture(work, "origin/dev", git_capture.Policy(5, 9))
    assert _names(kept) == ["a.md", "b.md"]
    assert kept.exclusions[-2:] == ("excluded size: 1", "excluded total: 1")
    excluded = git_capture.capture(work, "origin/dev", git_capture.Policy(5, 8))
    assert _names(excluded) == ["a.md", "d.md"]
    assert excluded.exclusions[-2:] == ("excluded size: 1", "excluded total: 1")


def test_file_modes_survive_a_restore(tmp_path):
    work = _pushed(tmp_path)
    for name, mode in (("run.sh", 0o700), ("other.sh", 0o601), ("plain.md", 0o600)):
        cases.write(work / name, name)
        (work / name).chmod(mode)
    target = _round_trip(tmp_path, git_capture.capture(work, "origin/dev"))
    assert (target / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (target / "other.sh").stat().st_mode & 0o777 == 0o644
    assert (target / "plain.md").stat().st_mode & 0o777 == 0o644


def test_capture_from_a_subdirectory_uses_the_worktree_root(tmp_path):
    work = _pushed(tmp_path)
    cases.write(work / "sub" / "draft.md", "draft\n")
    assert git_capture.capture(work / "sub", "origin/dev") == git_capture.capture(work, "origin/dev")


def test_patches_apply_under_hostile_diff_settings(tmp_path):
    work = _pushed(tmp_path)
    hostile = (("diff.noprefix", "true"), ("color.ui", "always"), ("diff.mnemonicPrefix", "true"))
    for key, value in hostile:
        cases.git(work, "config", key, value)
    cases.write(work / "app.py", "print('staged')\n")
    cases.git(work, "add", "app.py")
    cases.write(work / "app.py", "print('unstaged')\n")
    source = git_capture.capture(work, "origin/dev")
    for key, _ in hostile:
        cases.git(work, "config", "--unset", key)
    before = cases.state(work)
    target = _round_trip(tmp_path, source)
    assert cases.state(target) == before


def test_a_detached_head_is_refused(tmp_path):
    work = _pushed(tmp_path)
    cases.git(work, "checkout", "-q", "--detach")
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.capture(work, "origin/dev")
    assert str(raised.value) == "git symbolic-ref failed with exit 128"


def test_git_failures_name_the_command_and_exit_only(tmp_path):
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._git(tmp_path, "status")
    assert str(raised.value) == "git status failed with exit 128"
    assert git_capture._git(tmp_path, "hash-object", "--stdin", data=b"x\n").strip() == (
        b"587be6b4c3f93f93c489c0111bba5596147a26cb"
    )


def test_restore_refuses_a_checkpoint_that_is_not_authoritative(tmp_path):
    reader = model.Validator(cases.store(tmp_path / "durable"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0009", tmp_path)
    assert str(raised.value) == "checkpoint is not authoritative: manifest_absent"


def _committed(root: Path) -> tuple[model.Validator, Path]:
    work = cases.worktree(root)
    validator = model.Validator(cases.store(root / "durable"))
    assert cases.commit(validator, git_capture.capture(work, "origin/dev"), "ckpt-src-0001").authoritative
    return model.Validator(cases.store(root / "durable", execution="e2", generation=3)), work


def test_restore_refuses_a_dirty_target(tmp_path):
    reader, _ = _committed(tmp_path)
    target = cases.clone(tmp_path, "restored")
    cases.write(target / "mine.md", "mine\n")
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0001", target)
    assert str(raised.value) == "the restore target has changes of its own"
    assert cases.state(target)["untracked"] == ["mine.md"]


def test_restore_refuses_a_target_without_the_base(tmp_path):
    reader, _ = _committed(tmp_path)
    target = tmp_path / "unrelated"
    cases.git(tmp_path, "init", "-q", str(target))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(reader, "ckpt-src-0001", target)
    assert str(raised.value).startswith("git cat-file failed with exit ")


@pytest.mark.parametrize("staged", [True, False])
def test_restore_refuses_a_member_under_a_link_a_patch_creates(tmp_path, staged):
    work = _pushed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    cases.write(work / "lnk", "a regular file first\n")
    cases.git(work, "add", "lnk")
    cases.git(work, "commit", "-q", "-m", "regular file")
    (work / "lnk").unlink()
    (work / "lnk").symlink_to(outside)
    if staged:
        cases.git(work, "add", "lnk")
    source = git_capture.capture(work, "origin/dev")
    assert bool(source.materials[git_capture.STAGED]) is staged
    forged = dataclasses.replace(
        source, materials={**source.materials, git_capture.UNTRACKED: cases.forged("lnk/p.txt")}
    )
    validator = model.Validator(cases.store(tmp_path / "durable"))
    assert cases.commit(validator, forged, "ckpt-src-0001").authoritative
    target = cases.clone(tmp_path, "restored")
    before = (cases.state(target), cases.git(target, "branch", "--list"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(validator, "ckpt-src-0001", target)
    assert str(raised.value) == git_capture.ESCAPE
    assert (cases.state(target), cases.git(target, "branch", "--list")) == before
    assert list(outside.iterdir()) == []


def test_links_keep_a_tracked_link_name_that_holds_a_tab(tmp_path):
    work = _pushed(tmp_path)
    (work / "tab\tlink").symlink_to("app.py")
    cases.git(work, "add", "tab\tlink")
    cases.git(work, "commit", "-q", "-m", "link with a tab")
    head = cases.git(work, "rev-parse", "HEAD").strip()
    assert git_capture._links(work, head, ()) == {"tab\tlink"}


def test_restore_refuses_a_member_named_as_a_tracked_link(tmp_path):
    work = _pushed(tmp_path)
    (work / "hooks").symlink_to(".git/hooks")
    cases.git(work, "add", "hooks")
    cases.git(work, "commit", "-q", "-m", "link into the git folder")
    source = git_capture.capture(work, "origin/dev")
    forged = dataclasses.replace(source, materials={**source.materials, git_capture.UNTRACKED: cases.forged("hooks")})
    validator = model.Validator(cases.store(tmp_path / "durable"))
    assert cases.commit(validator, forged, "ckpt-src-0001").authoritative
    target = cases.clone(tmp_path, "restored")
    hooks = sorted(path.name for path in (target / ".git" / "hooks").iterdir())
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(validator, "ckpt-src-0001", target)
    assert str(raised.value) == git_capture.ESCAPE
    assert sorted(path.name for path in (target / ".git" / "hooks").iterdir()) == hooks


def test_restore_refuses_a_branch_name_git_rejects(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    validator = model.Validator(cases.store(tmp_path / "durable"))
    transcript = json.loads(cases.TRANSCRIPT.read_text())["materials"]["normalized_transcript"]
    validator.store.put("t.normalized_transcript", transcript.encode())
    for role, data in source.materials.items():
        validator.store.put(f"t.{role}", data)
    doc = {**git_capture.merged(_manifest("ckpt-src-0001"), source), "branch_name": "-bad..name"}
    assert validator.commit(doc).authoritative
    target = cases.clone(tmp_path, "restored")
    before = (cases.state(target), cases.git(target, "branch", "--list"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(validator, "ckpt-src-0001", target)
    assert str(raised.value).startswith("git check-ref-format failed with exit ")
    assert (cases.state(target), cases.git(target, "branch", "--list")) == before


def _archive(info: tarfile.TarInfo, data: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def _member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 1) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size if kind == tarfile.REGTYPE else 0
    if kind == tarfile.SYMTYPE:
        info.linkname = "/etc/passwd"
    return info


@pytest.mark.parametrize(
    "info",
    [
        _member("/abs.txt"),
        _member("../up.txt"),
        _member("a/../../up.txt"),
        _member(".git/hooks/post-checkout"),
        _member("nested/.git/config"),
        _member("link.txt", tarfile.SYMTYPE),
        _member("folder", tarfile.DIRTYPE),
    ],
)
def test_archive_members_outside_the_worktree_or_not_regular_are_refused(info):
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._members(_archive(info, b"x" if info.isreg() else b""))
    assert str(raised.value) == git_capture.ESCAPE


def _many(sizes: tuple[int, ...]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for index, size in enumerate(sizes):
            info = tarfile.TarInfo(f"m{index}")
            info.size = size
            archive.addfile(info, io.BytesIO(b"x" * size))
    return buffer.getvalue()


def test_member_count_and_expanded_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(git_capture, "ARCHIVE_MEMBERS", 2)
    policy = git_capture.Policy(total_limit=5)
    monkeypatch.setattr(git_capture, "Policy", lambda: policy)
    assert [name for name, _, _ in git_capture._members(_many((2, 3)))] == ["m0", "m1"]
    for sizes in ((1, 1, 1), (2, 4)):
        with pytest.raises(git_capture.SourceError) as raised:
            git_capture._members(_many(sizes))
        assert str(raised.value) == "the untracked archive holds more members or bytes than an extraction allows"


@pytest.mark.parametrize("extra", (0, 1))
def test_source_archive_reader_enforces_writer_total_limit(extra):
    policy = git_capture.Policy()
    sizes = (policy.file_limit,) * (policy.total_limit // policy.file_limit)
    if extra:
        with pytest.raises(git_capture.SourceError) as raised:
            git_capture._members(_many((*sizes, extra)))
        assert str(raised.value) == "the untracked archive holds more members or bytes than an extraction allows"
    else:
        members = git_capture._members(_many(sizes))
        assert sum(len(data) for _, _, data in members) == policy.total_limit


def test_a_compressed_archive_is_not_expanded():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"a"))
    with pytest.raises(tarfile.ReadError):
        git_capture._members(buffer.getvalue())


def test_regular_members_are_read_with_their_mode():
    info = _member("dir/run.sh", size=2)
    info.mode = 0o755
    assert git_capture._members(_archive(info, b"ok")) == [("dir/run.sh", 0o755, b"ok")]


def test_extraction_through_a_tracked_link_is_refused(tmp_path):
    target, outside = tmp_path / "target", tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "out").symlink_to(outside)
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture._extract(target, [("out/planted.txt", 0o644, b"x")])
    assert str(raised.value) == git_capture.ESCAPE
    assert list(outside.iterdir()) == []
    git_capture._extract(target, [("in/deep/kept.txt", 0o600, b"x")])
    assert (target / "in" / "deep" / "kept.txt").stat().st_mode & 0o777 == 0o600


def test_no_member_is_written_when_a_later_one_escapes(tmp_path):
    target, outside = tmp_path / "target", tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "out").symlink_to(outside)
    with pytest.raises(git_capture.SourceError):
        git_capture._extract(target, [("first.txt", 0o644, b"x"), ("out/planted.txt", 0o644, b"y")])
    assert sorted(path.name for path in target.iterdir()) == ["out"]


def _manifest(checkpoint_id: str) -> dict:
    bundle = json.loads(cases.TRANSCRIPT.read_text())
    return {**bundle["manifest"], "checkpoint_id": checkpoint_id, "operation_id": checkpoint_id}


def test_checkpoint_refuses_an_escaping_archive_before_storing_anything(tmp_path):
    source = git_capture.capture(cases.worktree(tmp_path), "origin/dev")
    forged = dataclasses.replace(source, materials={**source.materials, git_capture.UNTRACKED: cases.forged("../x")})
    validator = model.Validator(cases.store(tmp_path / "durable"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.checkpoint(validator, forged, _manifest("ckpt-src-0001"))
    assert str(raised.value) == git_capture.ESCAPE
    assert cases.keys(validator) == []


def test_an_attempt_outside_the_manifest_authority_stores_nothing(tmp_path):
    work = cases.worktree(tmp_path)
    durable = tmp_path / "durable"
    stale = model.Validator(cases.store(durable, generation=1))
    first = git_capture.capture(work, "origin/dev")
    assert git_capture.checkpoint(stale, first, _manifest("ckpt-src-0001")).reason == "scope_mismatch"
    assert cases.keys(stale) == []
    for key in ("task_id", "execution_id"):
        manifest = _manifest("ckpt-src-0001")
        manifest["authority"] = {**manifest["authority"], key: "other"}
        current = model.Validator(cases.store(durable))
        assert git_capture.checkpoint(current, first, manifest).reason == "scope_mismatch"
        assert cases.keys(current) == []
    cases.write(work / "draft.md", "changed after the stale attempt\n")
    current = model.Validator(cases.store(durable))
    assert cases.commit(current, git_capture.capture(work, "origin/dev"), "ckpt-src-0001").authoritative


def test_a_manifest_without_authority_stores_nothing(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    manifest = _manifest("ckpt-src-0001")
    del manifest["authority"]
    validator = model.Validator(cases.store(tmp_path / "durable"))
    assert git_capture.checkpoint(validator, source, manifest).reason == "invalid_record"
    assert cases.keys(validator) == []


def test_restore_refuses_a_checkpoint_without_every_source_material(tmp_path):
    source = git_capture.capture(_pushed(tmp_path), "origin/dev")
    validator = model.Validator(cases.store(tmp_path / "durable"))
    transcript = json.loads(cases.TRANSCRIPT.read_text())["materials"]["normalized_transcript"]
    validator.store.put("t.normalized_transcript", transcript.encode())
    for role, data in source.materials.items():
        validator.store.put(f"t.{role}", data)
    for role in sorted(git_capture.SOURCE_ROLES):
        doc = git_capture.merged(_manifest(f"ckpt-without-{role.replace('_', '-')}"), source)
        del doc["inventory"][role]
        del doc["artifact_hashes"][role]
        assert validator.commit(doc).authoritative
        target = cases.clone(tmp_path, role)
        with pytest.raises(git_capture.SourceError) as raised:
            git_capture.restore(validator, doc["checkpoint_id"], target)
        assert str(raised.value) == git_capture.INCOMPLETE


def test_restore_refuses_a_member_under_a_tracked_link_before_checkout(tmp_path):
    work = _pushed(tmp_path)
    (tmp_path / "outside").mkdir()
    (work / "sub").mkdir()
    (work / "sub" / "out").symlink_to(tmp_path / "outside")
    cases.write(work / "zz.txt", "after the link\n")
    cases.git(work, "add", "sub/out", "zz.txt")
    cases.git(work, "commit", "-q", "-m", "tracked link")
    source = git_capture.capture(work, "origin/dev")
    forged = dataclasses.replace(
        source, materials={**source.materials, git_capture.UNTRACKED: cases.forged("sub/out/planted.txt")}
    )
    validator = model.Validator(cases.store(tmp_path / "durable"))
    assert cases.commit(validator, forged, "ckpt-src-0001").authoritative
    target = cases.clone(tmp_path, "restored")
    before = (cases.state(target), cases.git(target, "branch", "--list"))
    with pytest.raises(git_capture.SourceError) as raised:
        git_capture.restore(validator, "ckpt-src-0001", target)
    assert str(raised.value) == git_capture.ESCAPE
    assert (cases.state(target), cases.git(target, "branch", "--list")) == before
    assert list((tmp_path / "outside").iterdir()) == []


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
