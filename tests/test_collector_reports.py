"""Phase 8 collector report listing/download: only bare *.jsonl names in
the fixed evidence directory are ever listed or served, byte-for-byte."""

import os

import pytest
from fastapi import HTTPException

from algoedge import collector_reports, web_server

RECORD = b'{"seq": 1, "status": {"ok": true}, "record_sha256": "abc"}\n'


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    directory = tmp_path / "campaign_status"
    directory.mkdir()
    monkeypatch.setattr(collector_reports, "COLLECTOR_EVIDENCE_DIR", directory)
    return directory


def write(directory, name, content=RECORD, mtime=None):
    path = directory / name
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_lists_only_jsonl_files_newest_first(evidence_dir) -> None:
    write(evidence_dir, "status_20261005T091500+0530_000000000001.jsonl", mtime=1_000)
    write(evidence_dir, "status_20261006T091500+0530_000000000002.jsonl", RECORD * 2, mtime=2_000)
    write(evidence_dir, "notes.txt")
    write(evidence_dir, ".env")
    (evidence_dir / "nested.jsonl").mkdir()

    files = web_server.phase8_collector_files()["files"]

    assert [file["name"] for file in files] == [
        "status_20261006T091500+0530_000000000002.jsonl",
        "status_20261005T091500+0530_000000000001.jsonl",
    ]
    assert files[0]["sizeBytes"] == len(RECORD) * 2
    assert files[0]["modifiedAt"].startswith("1970-01-01T00:33:20")


def test_empty_directory_lists_no_files(evidence_dir) -> None:
    assert web_server.phase8_collector_files() == {"files": []}


def test_missing_directory_lists_no_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(collector_reports, "COLLECTOR_EVIDENCE_DIR", tmp_path / "absent")

    assert web_server.phase8_collector_files() == {"files": []}


def test_download_returns_original_bytes_as_attachment(evidence_dir) -> None:
    name = "status_20261005T091500+0530_000000000001.jsonl"
    content = RECORD + b'{"seq": 2, "price": "NaN"}\n'
    write(evidence_dir, name, content)

    response = web_server.phase8_collector_file_download(name)

    assert response.body == content
    assert response.media_type == "application/x-ndjson"
    assert response.headers["content-disposition"] == f'attachment; filename="{name}"'


def test_download_of_nonexistent_file_is_404(evidence_dir) -> None:
    with pytest.raises(HTTPException) as error:
        web_server.phase8_collector_file_download("status_missing.jsonl")

    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../secret.jsonl",
        "..%2Fsecret.jsonl",
        "..",
        "a..b.jsonl",
        "/etc/passwd.jsonl",
        "sub/file.jsonl",
        "sub\\file.jsonl",
        ".hidden.jsonl",
        "",
    ],
)
def test_path_traversal_and_path_names_are_rejected(evidence_dir, name) -> None:
    (evidence_dir.parent / "secret.jsonl").write_bytes(b"outside")

    with pytest.raises(HTTPException) as error:
        web_server.phase8_collector_file_download(name)

    assert error.value.status_code == 400


@pytest.mark.parametrize("name", [".env", "notes.txt", "status.json", "status.jsonl.bak"])
def test_non_jsonl_files_are_rejected_even_if_present(evidence_dir, name) -> None:
    write(evidence_dir, name)

    with pytest.raises(HTTPException) as error:
        web_server.phase8_collector_file_download(name)

    assert error.value.status_code == 400


def test_symlink_pointing_outside_the_directory_is_neither_listed_nor_served(evidence_dir) -> None:
    outside = evidence_dir.parent / "outside.jsonl"
    outside.write_bytes(b"outside")
    (evidence_dir / "link.jsonl").symlink_to(outside)

    assert web_server.phase8_collector_files() == {"files": []}
    with pytest.raises(HTTPException) as error:
        web_server.phase8_collector_file_download("link.jsonl")
    assert error.value.status_code == 404


def test_file_deleted_after_listing_is_404(evidence_dir) -> None:
    path = write(evidence_dir, "status_gone.jsonl")
    assert len(web_server.phase8_collector_files()["files"]) == 1
    path.unlink()

    with pytest.raises(HTTPException) as error:
        web_server.phase8_collector_file_download("status_gone.jsonl")

    assert error.value.status_code == 404
