import json
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec import Struct
from pydantic import BaseModel

from lothc import HTTPClient, SyncHTTPClient


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class RenameBody(BaseModel):
    name: str


class EchoedHeaders(BaseModel):
    x_custom: str | None = None


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
        response_data_type=dict,
    )

    assert result["fields"] == {"note": "hello", "avatar": "raw-bytes"}
    assert result["files"] == [{"name": "doc", "filename": "doc.txt", "size": 12}]


async def test_post_with_form_path_file(client: HTTPClient, tmp_path: Path) -> None:
    upload_path = tmp_path / "upload.txt"
    upload_path.write_bytes(b"path-content")

    result = await client.post("upload", form={"doc": upload_path}, response_data_type=dict)

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
            response_data_type=dict,
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
        "upload", form={"blob": BytesIO(b"blob-content")}, response_data_type=dict
    )

    assert result["fields"] == {"blob": "blob-content"}
    assert result["files"] == []


async def test_post_with_form_file_mime_auto_inferred(client: HTTPClient) -> None:
    result = await client.post(
        "upload", form={"avatar": ("a.png", b"png-bytes")}, response_data_type=dict
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts == [
        {"name": "avatar", "filename": "a.png", "content_type": "image/png", "text": "png-bytes"}
    ]


async def test_post_with_form_file_explicit_mime_override(client: HTTPClient) -> None:
    result = await client.post(
        "upload",
        form={"avatar": ("a.png", b"png-bytes", "image/webp")},
        response_data_type=dict,
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "image/webp"


async def test_post_with_form_file_mime_inference_disabled(client: HTTPClient) -> None:
    result = await client.post(
        "upload",
        form={"avatar": ("a.png", b"png-bytes")},
        infer_mime_type_from_file_extension=False,
        response_data_type=dict,
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] is None


async def test_post_with_form_path_with_content_type_override(
    client: HTTPClient, tmp_path: Path
) -> None:
    upload_path = tmp_path / "upload.bin"
    upload_path.write_bytes(b"path-content")

    result = await client.post(
        "upload", form={"doc": (upload_path, "application/pdf")}, response_data_type=dict
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0] == {
        "name": "doc",
        "filename": "upload.bin",
        "content_type": "application/pdf",
        "text": "path-content",
    }


async def test_post_with_form_bare_path_mime_auto_inferred(
    client: HTTPClient, tmp_path: Path
) -> None:
    upload_path = tmp_path / "upload.pdf"
    upload_path.write_bytes(b"path-content")

    result = await client.post("upload", form={"doc": upload_path}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_buffered_io_mime_auto_inferred(
    client: HTTPClient, tmp_path: Path
) -> None:
    file_path = tmp_path / "opened.pdf"
    file_path.write_bytes(b"opened-content")

    with file_path.open("rb") as opened_file:
        result = await client.post("upload", form={"doc": opened_file}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_repeated_scalar_values(client: HTTPClient) -> None:
    result = await client.post("upload", form={"tag": (1, "x")}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


async def test_post_with_form_repeated_file_values(client: HTTPClient) -> None:
    result = await client.post(
        "upload",
        form={"photos": (("a.png", b"1"), ("b.png", b"2"))},
        response_data_type=dict,
    )

    files = cast(list[dict[str, Any]], result["files"])
    photo_files = [file for file in files if file["name"] == "photos"]
    assert {file["filename"] for file in photo_files} == {"a.png", "b.png"}


async def test_post_with_form_json_array_body(client: HTTPClient) -> None:
    result = await client.post("upload", form={"tags": ["a", "b"]}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == ["a", "b"]


async def test_post_with_form_json_object_body(client: HTTPClient) -> None:
    result = await client.post("upload", form={"meta": {"k": "v"}}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"k": "v"}


async def test_post_with_form_json_body_from_pydantic_model(client: HTTPClient) -> None:
    result = await client.post(
        "upload", form={"meta": ItemModel(id=1, name="ditto")}, response_data_type=dict
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"id": 1, "name": "ditto"}


async def test_post_with_form_json_body_from_msgspec_struct(client: HTTPClient) -> None:
    result = await client.post(
        "upload", form={"meta": ItemStruct(id=1, name="ditto")}, response_data_type=dict
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"id": 1, "name": "ditto"}


def test_sync_post_with_form_file_explicit_mime_override(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "upload",
        form={"avatar": ("a.png", b"png-bytes", "image/webp")},
        response_data_type=dict,
    )

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "image/webp"


def test_sync_post_with_form_repeated_scalar_values(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tag": (1, "x")}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


def test_sync_post_with_form_json_array_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tags": ["a", "b"]}, response_data_type=dict)

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == ["a", "b"]


async def test_post_with_form_unsupported_value_type_raises_type_error(
    client: HTTPClient,
) -> None:
    # Not a documented/supported Form value — bypasses static typing via cast(Any, ...).
    with pytest.raises(TypeError, match="Unsupported form value for 'bogus'"):
        await client.post(
            "upload",
            form={"note": "hello", "bogus": cast(Any, {1, 2, 3})},
            response_data_type=dict,
        )


def test_sync_post_with_form_unsupported_value_type_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Unsupported form value for 'bogus'"):
        sync_client.post(
            "upload",
            form={"note": "hello", "bogus": cast(Any, {1, 2, 3})},
            response_data_type=dict,
        )


async def test_post_with_form_unsupported_value_inside_repeated_tuple_raises_type_error(
    client: HTTPClient,
) -> None:
    # A tuple that isn't one of the File shapes means "repeat this name" — an unsupported
    # value among its elements must still raise, not be silently dropped.
    with pytest.raises(TypeError, match="Unsupported form value for 'bogus'"):
        await client.post(
            "upload",
            form={"bogus": cast(Any, ("ok", {1, 2, 3}))},
            response_data_type=dict,
        )


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
            },
            response_data_type=dict,
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


async def test_post_result_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.post_result(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_post_result_with_typed_headers(client: HTTPClient) -> None:
    result = await client.post_result(
        "items",
        json=ItemModel(id=1, name="ditto"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_post_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post_result(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_post_result_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.post_result("missing", error_for_status=False)

    assert result.status == 404


async def test_put_result_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.put_result(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_put_result_with_typed_headers(client: HTTPClient) -> None:
    result = await client.put_result(
        "items/7",
        json=ItemModel(id=0, name="replaced"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_put_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.put_result(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_patch_result_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.patch_result(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


async def test_patch_result_with_typed_headers(client: HTTPClient) -> None:
    result = await client.patch_result(
        "items/7",
        json=RenameBody(name="renamed"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_patch_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.patch_result(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


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
    result = await client.post("echo-body", content="hello", response_data_type=dict)

    assert result["body"] == "hello"


async def test_post_with_bytes_content(client: HTTPClient) -> None:
    result = await client.post("echo-body", content=b"hello-bytes", response_data_type=dict)

    assert result["body"] == "hello-bytes"


async def test_post_with_more_than_one_body_kind_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one"):
        await client.post("items", json={"id": 1}, content="also-content")


def test_sync_post_with_string_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content="hello", response_data_type=dict)

    assert result["body"] == "hello"


def test_sync_post_with_bytes_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content=b"hello-bytes", response_data_type=dict)

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
