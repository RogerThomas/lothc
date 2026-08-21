from pathlib import Path

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
