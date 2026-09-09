"""Ledger recovery must never mutate aliases outside its selected run directory."""

from __future__ import annotations

import hashlib
import os

import pytest
from ledger import Ledger, LedgerError


def _record(model="persistence"):
    return {"record_type": "forecast", "model": model, "origin_date": "2021-06-01"}


def _checkpoint(tmp_path):
    run = tmp_path / "run"
    ledger = Ledger.create(run)
    ledger.append([_record()])
    recorded = ledger.fingerprints()
    shard = run / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
    return run, ledger, recorded, shard


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_resume_refuses_external_shard_alias_without_truncating_target(tmp_path, alias):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    external = tmp_path / "external"
    before = shard.read_bytes() + b"uncheckpointed tail"
    external.write_bytes(before)
    shard.unlink()
    if alias == "symlink":
        shard.symlink_to(external)
    else:
        os.link(external, shard)
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.resume(run, recorded)
    assert external.read_bytes() == before


@pytest.mark.parametrize("directory", ["ledger", "model"])
def test_resume_refuses_directory_alias_including_empty_checkpoint(tmp_path, directory):
    run, _ledger, _recorded, shard = _checkpoint(tmp_path)
    original = shard.parent.parent if directory == "ledger" else shard.parent
    external = tmp_path / "external"
    original.rename(external)
    original.symlink_to(external, target_is_directory=True)
    external_shard = external / ("persistence/forecast_2021.jsonl.gz" if directory == "ledger"
                                else "forecast_2021.jsonl.gz")
    before = external_shard.read_bytes()
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.resume(run, {})
    assert external_shard.read_bytes() == before


def test_resume_hashes_and_truncates_same_open_file_despite_leaf_swap(tmp_path, monkeypatch):
    run, ledger, recorded, shard = _checkpoint(tmp_path)
    ledger.append([_record()])
    original = shard.with_name("original")
    external = tmp_path / "external"
    before = b"external bytes must survive"
    external.write_bytes(before)
    real_sha256 = hashlib.sha256

    class SwapAfterRead:
        def __init__(self):
            self.hasher = real_sha256()

        def update(self, data):
            self.hasher.update(data)
            if shard.exists() and not shard.is_symlink():
                shard.rename(original)
                shard.symlink_to(external)

        def hexdigest(self):
            return self.hasher.hexdigest()

    monkeypatch.setattr(hashlib, "sha256", SwapAfterRead)
    Ledger.resume(run, recorded)
    assert external.read_bytes() == before
    assert original.stat().st_size == recorded["persistence/forecast_2021.jsonl.gz"]["size"]


def test_unrecorded_cleanup_pins_parent_when_name_is_swapped(tmp_path, monkeypatch):
    run, _ledger, _recorded, shard = _checkpoint(tmp_path)
    parent = shard.parent
    moved = parent.with_name("moved")
    external = tmp_path / "external"
    external.mkdir()
    target = external / shard.name
    target.write_bytes(b"external must survive")
    real_unlink = os.unlink
    swapped = False

    def swap_then_unlink(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(path).endswith(shard.name):
            swapped = True
            parent.rename(moved)
            parent.symlink_to(external, target_is_directory=True)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", swap_then_unlink)
    Ledger.resume(run, {})
    assert target.read_bytes() == b"external must survive"
    assert not (moved / shard.name).exists()


@pytest.mark.parametrize("key", ["../outside.jsonl.gz", "/outside.jsonl.gz", "C:/outside.jsonl.gz",
                                 "persistence\\forecast_2021.jsonl.gz", "a/../forecast_2021.jsonl.gz"])
def test_resume_rejects_noncanonical_checkpoint_key(tmp_path, key):
    run, _ledger, _recorded, _shard = _checkpoint(tmp_path)
    with pytest.raises(LedgerError, match="REFUSED.*path"):
        Ledger.resume(run, {key: {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}})


@pytest.mark.parametrize("size", [-1, True, "0", 0.5])
def test_resume_rejects_invalid_checkpoint_size(tmp_path, size):
    run, _ledger, recorded, _shard = _checkpoint(tmp_path)
    recorded["persistence/forecast_2021.jsonl.gz"]["size"] = size
    with pytest.raises(LedgerError, match="REFUSED.*size"):
        Ledger.resume(run, recorded)


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_append_after_resume_rejects_replaced_shard_alias(tmp_path, alias):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    resumed = Ledger.resume(run, recorded)
    external = tmp_path / "external"
    external.write_bytes(b"external must survive")
    shard.unlink()
    if alias == "symlink":
        shard.symlink_to(external)
    else:
        os.link(external, shard)
    with pytest.raises(LedgerError, match="REFUSED"):
        resumed.append([_record()])
    assert external.read_bytes() == b"external must survive"


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_resume_refuses_shard_replaced_immediately_before_open(tmp_path, monkeypatch, replacement):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    external = tmp_path / "external"
    external.write_bytes(shard.read_bytes() + b"external tail")
    before = external.read_bytes()
    real_open = os.open
    replaced = False

    def swap_then_open(path, *args, **kwargs):
        nonlocal replaced
        if not replaced and str(path).endswith(shard.name):
            replaced = True
            shard.unlink()
            if replacement == "symlink":
                shard.symlink_to(external)
            else:
                os.mkfifo(shard)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.resume(run, recorded)
    assert replaced
    assert external.read_bytes() == before


def test_resume_refuses_model_directory_replaced_between_scan_and_open(tmp_path, monkeypatch):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    target = external / shard.name
    target.write_bytes(shard.read_bytes() + b"external tail")
    before = target.read_bytes()
    model_dir = shard.parent
    real_open = os.open
    model_opens = 0

    def swap_then_open(path, *args, **kwargs):
        nonlocal model_opens
        if str(path) == "persistence":
            model_opens += 1
            if model_opens == 2:
                model_dir.rename(model_dir.with_name("original"))
                model_dir.symlink_to(external, target_is_directory=True)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.resume(run, recorded)
    assert model_opens == 2
    assert target.read_bytes() == before


def test_readonly_verification_rejects_symlink_without_changing_bytes(tmp_path):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    external = tmp_path / "external"
    before = shard.read_bytes() + b"external tail"
    shard.rename(external)
    external.write_bytes(before)
    shard.symlink_to(external)
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.verify_shards(run, recorded)
    assert external.read_bytes() == before


def test_append_rejects_model_path_escape(tmp_path):
    ledger = Ledger.create(tmp_path / "run")
    with pytest.raises(LedgerError, match="REFUSED.*path"):
        ledger.append([_record("../../outside")])
    assert not (tmp_path / "outside").exists()


def test_create_rejects_ledger_directory_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    (run / "ledger").symlink_to(external, target_is_directory=True)
    with pytest.raises(LedgerError, match="REFUSED"):
        Ledger.create(run)
    assert not list(external.iterdir())


def test_missing_secure_platform_primitives_fail_closed(tmp_path, monkeypatch):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    before = shard.read_bytes()
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(LedgerError, match="REFUSED.*platform"):
        Ledger.resume(run, recorded)
    assert shard.read_bytes() == before


def test_replaced_selected_run_directory_is_not_reauthorized(tmp_path):
    run, ledger, _recorded, _shard = _checkpoint(tmp_path)
    external = tmp_path / "external"
    external_ledger = Ledger.create(external)
    external_ledger.append([_record()])
    target = external / "ledger" / "persistence" / "forecast_2021.jsonl.gz"
    before = target.read_bytes()
    run.rename(tmp_path / "original")
    run.symlink_to(external, target_is_directory=True)
    with pytest.raises(LedgerError, match="REFUSED"):
        ledger.append([_record()])
    assert target.read_bytes() == before


def test_creation_pins_parent_before_creating_child(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    original = tmp_path / "original"
    selected = parent / "run"
    real_mkdir = os.mkdir
    swapped = False

    def swap_then_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and str(path) in (str(selected), "run"):
            swapped = True
            parent.rename(original)
            parent.symlink_to(external, target_is_directory=True)
        return real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap_then_mkdir)
    Ledger.create(selected)
    assert swapped
    assert not (external / "run").exists()
    assert (original / "run" / "ledger").is_dir()


def test_nested_empty_directories_are_refused(tmp_path):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    (shard.parent / "nested").mkdir()
    with pytest.raises(LedgerError, match="REFUSED.*path"):
        Ledger.resume(run, recorded)


def test_operator_selected_run_alias_remains_bound_to_original_target(tmp_path):
    run, _ledger, recorded, shard = _checkpoint(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(run, target_is_directory=True)
    ledger = Ledger.resume(alias, recorded)
    external = tmp_path / "external"
    Ledger.create(external)
    alias.unlink()
    alias.symlink_to(external, target_is_directory=True)
    before = shard.stat().st_size
    ledger.append([_record()])
    assert shard.stat().st_size > before
    assert not list((external / "ledger").iterdir())
