import gzip
import os
import time
from pathlib import Path

import pytest

from ingest import files


@pytest.mark.parametrize("name, ok", [
    ("sales.csv", True), ("SALES.CSV", True), ("sales.csv.part", False),
    ("sales.csv.crdownload", False), (".sales.csv", False), ("sales.xlsx", False),
    ("sales.tmp", False),
])
def test_is_candidate(name, ok):
    assert files.is_candidate(Path(name)) is ok


def test_provider_of():
    inbox = Path("/data/inbox")
    assert files.provider_of(inbox / "copart" / "a.csv", inbox) == "copart"
    assert files.provider_of(inbox / "a.csv", inbox) is None
    assert files.provider_of(inbox / "copart" / "sub" / "a.csv", inbox) is None
    assert files.provider_of(Path("/elsewhere/copart/a.csv"), inbox) is None


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_until_stable_true_once_file_stops_changing(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("x")
    clock = FakeClock()
    assert files.wait_until_stable(path, 2, poll=0.5, clock=clock, sleep=clock.sleep)
    assert clock.now >= 2


def test_wait_until_stable_restarts_while_file_grows(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("x")
    clock = FakeClock()
    writes = iter(range(3))

    def sleep(seconds):
        clock.sleep(seconds)
        if next(writes, None) is not None:
            with open(path, "a") as fh:
                fh.write("more")

    assert files.wait_until_stable(path, 2, poll=0.5, clock=clock, sleep=sleep)
    assert clock.now >= 3.5  # 3 growing polls, then 2s of quiet


def test_wait_until_stable_false_when_file_vanishes_or_never_settles(tmp_path):
    assert not files.wait_until_stable(tmp_path / "missing.csv", 1)
    path = tmp_path / "a.csv"
    path.write_text("x")
    clock = FakeClock()

    def sleep(seconds):
        clock.sleep(seconds)
        with open(path, "a") as fh:
            fh.write("more")

    assert not files.wait_until_stable(path, 2, poll=1, timeout=5, clock=clock, sleep=sleep)


def test_wait_until_stable_waits_for_empty_file_to_get_content(tmp_path):
    path = tmp_path / "a.csv"
    path.touch()
    clock = FakeClock()
    assert not files.wait_until_stable(path, 1, poll=0.5, timeout=3, clock=clock,
                                       sleep=clock.sleep)


def test_archive_gzips_and_removes_original(tmp_path):
    src = tmp_path / "inbox" / "a.csv"
    src.parent.mkdir()
    src.write_text("hello")
    target = files.archive(src, tmp_path / "archive", "copart", 7)
    assert not src.exists()
    assert target.name == "7_a.csv.gz"
    assert target.parent.parent.parent.parent == tmp_path / "archive" / "copart"
    assert gzip.decompress(target.read_bytes()) == b"hello"


def test_move_to(tmp_path):
    src = tmp_path / "a.csv"
    src.write_text("x")
    target = files.move_to(src, tmp_path / "failed", "copart", 3)
    assert target == tmp_path / "failed" / "copart" / "3_a.csv"
    assert target.read_text() == "x"


def test_purge_archive_keeps_recent_files(tmp_path):
    old, new = tmp_path / "a" / "old.gz", tmp_path / "a" / "new.gz"
    old.parent.mkdir()
    old.write_text("x")
    new.write_text("x")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    assert files.purge_archive(tmp_path, retention_days=7) == 1
    assert not old.exists() and new.exists()


def test_sha256_of(tmp_path):
    path = tmp_path / "a"
    path.write_bytes(b"abc")
    assert files.sha256_of(path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
