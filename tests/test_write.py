from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec import Struct
from pydantic import BaseModel

from lothc import JSON, HTTPClient, SyncHTTPClient


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class RenameBody(BaseModel):
    name: str


async def test_post_with_pydantic_json_body(client: HTTPClient) -> None:
    item = await client.post(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=1, name="ditto")


async def test_post_with_msgspec_json_body(client: HTTPClient) -> None:
    item = await client.post(
        "items", json=ItemStruct(id=1, name="ditto"), response_data_type=ItemStruct
    )

    assert item == ItemStruct(id=1, name="ditto")


async def test_post_with_raw_dict_json_body(client: HTTPClient) -> None:
    item = await client.post("items", json={"id": 1, "name": "ditto"}, response_data_type=ItemModel)

    assert item == ItemModel(id=1, name="ditto")


async def test_post_with_form_fields_and_files(client: HTTPClient) -> None:
    result = await client.post(
        "upload",
        form={"note": "hello", "avatar": b"raw-bytes", "doc": ("doc.txt", b"file-content")},
        response_data_type=JSON,
    )

    assert result["fields"] == {"note": "hello", "avatar": "raw-bytes"}
    assert result["files"] == [{"name": "doc", "filename": "doc.txt", "size": 12}]


async def test_post_with_form_path_file(client: HTTPClient, tmp_path: Path) -> None:
    upload_path = tmp_path / "upload.txt"
    upload_path.write_bytes(b"path-content")

    result = await client.post("upload", form={"doc": upload_path}, response_data_type=JSON)

    assert result["files"] == [{"name": "doc", "filename": "upload.txt", "size": 12}]


async def test_post_with_form_int_and_buffered_file(client: HTTPClient, tmp_path: Path) -> None:
    file_path = tmp_path / "opened.txt"
    file_path.write_bytes(b"opened-content")

    with file_path.open("rb") as opened_file:
        result = await client.post(
            "upload",
            # A tuple value ("filename", ("doc.txt", b"file-content")) followed by another
            # field exercises the loop continuing after the tuple `match` arm, not just the
            # arm's body itself.
            form={
                "page": 3,
                "doc": ("doc.txt", b"file-content"),
                "avatar": opened_file,
            },
            response_data_type=JSON,
        )

    assert result["fields"] == {"page": "3"}
    files = {file["name"]: file for file in result["files"]}
    assert files["doc"] == {"name": "doc", "filename": "doc.txt", "size": 12}
    assert files["avatar"] == {"name": "avatar", "filename": "opened.txt", "size": 14}


async def test_post_with_form_buffered_value_without_a_name_attribute(
    client: HTTPClient,
) -> None:
    # A BufferedIOBase with no `.name` attribute (e.g. an in-memory BytesIO, unlike a real
    # opened file) must still upload — just without a filename on the multipart part, which
    # this server surfaces as a plain field rather than a file entry.
    result = await client.post(
        "upload", form={"blob": BytesIO(b"blob-content")}, response_data_type=JSON
    )

    assert result["fields"] == {"blob": "blob-content"}
    assert result["files"] == []


async def test_post_with_form_unsupported_value_type_is_silently_skipped(
    client: HTTPClient,
) -> None:
    # Not a documented/supported Form value — bypasses static typing via cast(Any, ...).
    # Current behavior silently drops it rather than raising; asserting on that here so any
    # future change to this behavior is a deliberate, visible one.
    result = await client.post(
        "upload",
        form={"note": "hello", "bogus": cast(Any, [1, 2, 3])},
        response_data_type=JSON,
    )

    assert result["fields"] == {"note": "hello"}


def test_sync_post_with_form_all_value_types(sync_client: SyncHTTPClient, tmp_path: Path) -> None:
    path_file = tmp_path / "path-upload.txt"
    path_file.write_bytes(b"path-content")
    opened_path = tmp_path / "opened.txt"
    opened_path.write_bytes(b"opened-content")

    with opened_path.open("rb") as opened_file:
        result = sync_client.post(
            "upload",
            form={
                "page": 3,
                "doc": ("doc.txt", b"file-content"),
                "note": "hello",
                "avatar": b"raw-bytes",
                "path_doc": path_file,
                "opened_doc": opened_file,
                "blob": BytesIO(b"blob-content"),
                "bogus": cast(Any, [1, 2, 3]),
            },
            response_data_type=JSON,
        )

    assert result["fields"] == {
        "page": "3",
        "note": "hello",
        "avatar": "raw-bytes",
        "blob": "blob-content",
    }
    files = {file["name"]: file for file in result["files"]}
    assert files["doc"] == {"name": "doc", "filename": "doc.txt", "size": 12}
    assert files["path_doc"] == {"name": "path_doc", "filename": "path-upload.txt", "size": 12}
    assert files["opened_doc"] == {"name": "opened_doc", "filename": "opened.txt", "size": 14}


async def test_put_replaces_item(client: HTTPClient) -> None:
    item = await client.put(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=7, name="replaced")


async def test_patch_renames_item(client: HTTPClient) -> None:
    item = await client.patch(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=7, name="renamed")


def test_sync_post_with_pydantic_json_body(sync_client: SyncHTTPClient) -> None:
    item = sync_client.post(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=1, name="ditto")


def test_sync_put_replaces_item(sync_client: SyncHTTPClient) -> None:
    item = sync_client.put(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=7, name="replaced")


def test_sync_patch_renames_item(sync_client: SyncHTTPClient) -> None:
    item = sync_client.patch(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=7, name="renamed")


async def test_post_with_string_content(client: HTTPClient) -> None:
    result = await client.post("echo-body", content="hello", response_data_type=JSON)

    assert result["body"] == "hello"


async def test_post_with_bytes_content(client: HTTPClient) -> None:
    result = await client.post("echo-body", content=b"hello-bytes", response_data_type=JSON)

    assert result["body"] == "hello-bytes"


async def test_post_with_more_than_one_body_kind_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one"):
        await client.post("items", json={"id": 1}, content="also-content")


def test_sync_post_with_string_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content="hello", response_data_type=JSON)

    assert result["body"] == "hello"


def test_sync_post_with_bytes_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content=b"hello-bytes", response_data_type=JSON)

    assert result["body"] == "hello-bytes"


def test_sync_post_with_msgspec_json_body(sync_client: SyncHTTPClient) -> None:
    item = sync_client.post(
        "items", json=ItemStruct(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert item == ItemModel(id=1, name="ditto")


def test_sync_post_with_more_than_one_body_kind_raises_value_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(ValueError, match="at most one"):
        sync_client.post("items", json={"id": 1}, content="also-content")
