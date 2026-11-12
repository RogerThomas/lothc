import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from _helpers import (
    echoed_headers,
    make_unreadable,
    opens_but_fails_to_read,
    post_failing_form,
    sync_post_failing_form,
)
from msgspec import Struct
from pydantic import BaseModel, ConfigDict, Field

from lothc import Form, HTTPClient, SyncHTTPClient


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class AliasedModel(BaseModel):
    user_id: int = Field(alias="userId")


class ExplicitFieldNamesModel(BaseModel):
    model_config = ConfigDict(serialize_by_alias=False, validate_by_name=True)
    user_id: int = Field(alias="userId")


class AliasedStruct(Struct, rename="camel"):
    user_id: int


class AliasedHeaders(BaseModel):
    trace_id: str = Field(alias="X-Trace-Id")


class RenameBody(BaseModel):
    name: str


class EchoedHeaders(BaseModel):
    content_type: str


# `/upload`'s response (see `_handle_upload` in `_server.py`).
class UploadedFile(TypedDict):
    name: str
    filename: str
    size: int


class UploadedPart(TypedDict):
    name: str
    filename: str | None
    content_type: str | None
    text: str


class UploadResult(TypedDict):
    fields: dict[str, str]
    files: list[UploadedFile]
    parts: list[UploadedPart]


class _BytearrayReader(BytesIO):
    """A stream whose `read()` gives a `bytearray`, which pyreqwest accepts like `bytes`."""

    def read(self, size: int | None = -1) -> bytes:
        # Typed as Any: a `bytearray` breaks `read()`'s declared contract, which is the point.
        data: Any = bytearray(super().read(size))
        return data


async def test_post_with_msgspec_json_body(client: HTTPClient) -> None:
    item = (await client.post("items", json=ItemStruct(id=1, name="ditto"), into=ItemStruct)).data

    assert item == ItemStruct(id=1, name="ditto")


async def test_post_with_raw_dict_json_body(client: HTTPClient) -> None:
    item = (await client.post("items", json={"id": 1, "name": "ditto"}, into=ItemModel)).data

    assert item == ItemModel(id=1, name="ditto")


async def test_post_with_form_fields_and_files(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"note": "hello", "avatar": b"raw-bytes", "doc": ("doc.txt", b"file-content")},
            into=UploadResult,
        )
    ).data

    assert result["fields"] == {"note": "hello", "avatar": "raw-bytes"}
    assert result["files"] == [{"name": "doc", "filename": "doc.txt", "size": 12}]


async def test_post_with_form_path_file(client: HTTPClient, tmp_path: Path) -> None:
    upload_path = tmp_path / "upload.txt"
    upload_path.write_bytes(b"path-content")

    result = (await client.post("upload", form={"doc": upload_path}, into=UploadResult)).data

    assert result["files"] == [{"name": "doc", "filename": "upload.txt", "size": 12}]


async def test_post_with_form_int_and_buffered_file(client: HTTPClient, tmp_path: Path) -> None:
    file_path = tmp_path / "opened.txt"
    file_path.write_bytes(b"opened-content")

    with file_path.open("rb") as opened_file:
        result = (
            await client.post(
                "upload",
                # A tuple value ("filename", ("doc.txt", b"file-content")) followed by another
                # field exercises the loop continuing after the tuple `match` arm, not just the
                # arm's body itself.
                form={
                    "page": 3,
                    "doc": ("doc.txt", b"file-content"),
                    "avatar": opened_file,
                },
                into=UploadResult,
            )
        ).data

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
    result = (
        await client.post("upload", form={"blob": BytesIO(b"blob-content")}, into=UploadResult)
    ).data

    assert result["fields"] == {"blob": "blob-content"}
    assert result["files"] == []


async def test_post_with_form_buffered_value_whose_read_gives_a_bytearray(
    client: HTTPClient,
) -> None:
    result = (
        await client.post(
            "upload", form={"blob": _BytearrayReader(b"blob-content")}, into=UploadResult
        )
    ).data

    assert result["fields"] == {"blob": "blob-content"}


def test_sync_post_with_form_buffered_value_whose_read_gives_a_bytearray(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.post(
        "upload", form={"blob": _BytearrayReader(b"blob-content")}, into=UploadResult
    ).data

    assert result["fields"] == {"blob": "blob-content"}


async def test_post_with_form_file_mime_auto_inferred(client: HTTPClient) -> None:
    result = (
        await client.post("upload", form={"avatar": ("a.png", b"png-bytes")}, into=UploadResult)
    ).data

    parts = result["parts"]
    assert parts == [
        {"name": "avatar", "filename": "a.png", "content_type": "image/png", "text": "png-bytes"}
    ]


async def test_post_with_form_file_explicit_mime_override(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"avatar": ("a.png", b"png-bytes", "image/webp")},
            into=UploadResult,
        )
    ).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "image/webp"


async def test_post_with_form_file_mime_inference_disabled(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"avatar": ("a.png", b"png-bytes")},
            infer_mime_type_from_file_extension=False,
            into=UploadResult,
        )
    ).data

    parts = result["parts"]
    assert parts[0]["content_type"] is None


async def test_post_with_form_path_with_content_type_override(
    client: HTTPClient, tmp_path: Path
) -> None:
    upload_path = tmp_path / "upload.bin"
    upload_path.write_bytes(b"path-content")

    result = (
        await client.post(
            "upload", form={"doc": (upload_path, "application/pdf")}, into=UploadResult
        )
    ).data

    parts = result["parts"]
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

    result = (await client.post("upload", form={"doc": upload_path}, into=UploadResult)).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_buffered_io_mime_auto_inferred(
    client: HTTPClient, tmp_path: Path
) -> None:
    file_path = tmp_path / "opened.pdf"
    file_path.write_bytes(b"opened-content")

    with file_path.open("rb") as opened_file:
        result = (await client.post("upload", form={"doc": opened_file}, into=UploadResult)).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_repeated_scalar_values(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"tag": (1, "x")}, into=UploadResult)).data

    parts = result["parts"]
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


async def test_post_with_form_repeated_file_values(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"photos": (("a.png", b"1"), ("b.png", b"2"))},
            into=UploadResult,
        )
    ).data

    files = result["files"]
    photo_files = [file for file in files if file["name"] == "photos"]
    assert {file["filename"] for file in photo_files} == {"a.png", "b.png"}


async def test_post_with_form_json_array_body(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"tags": ["a", "b"]}, into=UploadResult)).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(parts[0]["text"]) == ["a", "b"]


async def test_post_with_form_json_object_body(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"meta": {"k": "v"}}, into=UploadResult)).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(parts[0]["text"]) == {"k": "v"}


async def test_post_with_form_json_body_from_pydantic_model(client: HTTPClient) -> None:
    result = (
        await client.post("upload", form={"meta": ItemModel(id=1, name="ditto")}, into=UploadResult)
    ).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(parts[0]["text"]) == {"id": 1, "name": "ditto"}


async def test_post_with_form_json_body_from_msgspec_struct(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload", form={"meta": ItemStruct(id=1, name="ditto")}, into=UploadResult
        )
    ).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(parts[0]["text"]) == {"id": 1, "name": "ditto"}


def test_sync_post_with_form_file_explicit_mime_override(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "upload",
        form={"avatar": ("a.png", b"png-bytes", "image/webp")},
        into=UploadResult,
    ).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "image/webp"


def test_sync_post_with_form_repeated_scalar_values(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tag": (1, "x")}, into=UploadResult).data

    parts = result["parts"]
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


def test_sync_post_with_form_json_array_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tags": ["a", "b"]}, into=UploadResult).data

    parts = result["parts"]
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(parts[0]["text"]) == ["a", "b"]


async def test_post_with_form_unsupported_value_type_raises_type_error(
    client: HTTPClient,
) -> None:
    # Not a documented/supported Form value — bypasses static typing via cast(Any, ...).
    with pytest.raises(TypeError, match="Unsupported form value for 'bogus'"):
        await client.post(
            "upload",
            form={"note": "hello", "bogus": cast(Any, {1, 2, 3})},
            into=UploadResult,
        )


def test_sync_post_with_form_unsupported_value_type_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Unsupported form value for 'bogus'"):
        sync_client.post(
            "upload",
            form={"note": "hello", "bogus": cast(Any, {1, 2, 3})},
            into=UploadResult,
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
            into=UploadResult,
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
                "typed_path_doc": (path_file, "text/plain"),
                "opened_doc": opened_file,
                "blob": BytesIO(b"blob-content"),
                "meta": {"key": "value"},
            },
            into=UploadResult,
        ).data

    assert result["fields"] == {
        "page": "3",
        "note": "hello",
        "avatar": "raw-bytes",
        "blob": "blob-content",
        # A JSON part carries no filename, so the server counts it a field, not a file.
        "meta": '{"key": "value"}',
    }
    files = {file["name"]: file for file in result["files"]}
    assert files["doc"] == {"name": "doc", "filename": "doc.txt", "size": 12}
    assert files["path_doc"] == {"name": "path_doc", "filename": "path-upload.txt", "size": 12}
    assert files["opened_doc"] == {"name": "opened_doc", "filename": "opened.txt", "size": 14}
    parts = {part["name"]: part for part in result["parts"]}
    # A (Path, content_type) pair keeps the path's own filename and overrides only the mime.
    assert parts["typed_path_doc"]["filename"] == "path-upload.txt"
    assert parts["typed_path_doc"]["content_type"] == "text/plain"
    assert parts["meta"]["content_type"] == "application/json"
    assert json.loads(parts["meta"]["text"]) == {"key": "value"}


async def test_response_post_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.post("items", json=ItemModel(id=1, name="ditto"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_response_post_with_typed_headers(client: HTTPClient) -> None:
    result = await client.post(
        "items",
        json=ItemModel(id=1, name="ditto"),
        into=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers == EchoedHeaders(content_type="application/json")


def test_sync_post_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("items", json=ItemModel(id=1, name="ditto"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_response_post_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.post("missing", error_for_status=False)

    assert result.status == 404


async def test_response_put_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.put("items/7", json=ItemModel(id=0, name="replaced"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_response_put_with_typed_headers(client: HTTPClient) -> None:
    result = await client.put(
        "items/7",
        json=ItemModel(id=0, name="replaced"),
        into=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers == EchoedHeaders(content_type="application/json")


def test_sync_put_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.put("items/7", json=ItemModel(id=0, name="replaced"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_response_patch_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.patch("items/7", json=RenameBody(name="renamed"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


async def test_response_patch_with_typed_headers(client: HTTPClient) -> None:
    result = await client.patch(
        "items/7",
        json=RenameBody(name="renamed"),
        into=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers == EchoedHeaders(content_type="application/json")


def test_sync_patch_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.patch("items/7", json=RenameBody(name="renamed"), into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


async def test_post_with_string_content(client: HTTPClient) -> None:
    result = (await client.post("echo-body", content="hello", into=dict)).data

    assert result["body"] == "hello"
    assert result["content_types"] == []  # unlike httpx, no default text/plain


async def test_post_with_bytes_content(client: HTTPClient) -> None:
    result = (await client.post("echo-body", content=b"hello-bytes", into=dict)).data

    assert result["body"] == "hello-bytes"
    assert result["content_types"] == []  # unlike httpx, no default text/plain


async def test_post_with_more_than_one_body_kind_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one"):
        await client.post("items", json={"id": 1}, content="also-content")


def test_sync_post_with_string_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content="hello", into=dict).data

    assert result["body"] == "hello"
    assert result["content_types"] == []  # unlike httpx, no default text/plain


def test_sync_post_with_bytes_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content=b"hello-bytes", into=dict).data

    assert result["body"] == "hello-bytes"
    assert result["content_types"] == []  # unlike httpx, no default text/plain


def test_sync_post_with_msgspec_json_body(sync_client: SyncHTTPClient) -> None:
    item = sync_client.post("items", json=ItemStruct(id=1, name="ditto"), into=ItemModel).data

    assert item == ItemModel(id=1, name="ditto")


def test_sync_post_with_more_than_one_body_kind_raises_value_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(ValueError, match="at most one"):
        sync_client.post("items", json={"id": 1}, content="also-content")


async def test_post_with_form_mixed_kinds_in_repeated_tuple_raises_type_error(
    client: HTTPClient,
) -> None:
    # (bytes, content_type) reads like a file but has no filename to be one, so it would
    # otherwise repeat as a binary part plus a text part reading "text/plain" — bypasses static
    # typing via cast(Any, ...), since _FormRepeat already rejects it at type-check time.
    with pytest.raises(TypeError, match="Mixed form value kinds for 'avatar'"):
        await client.post(
            "upload",
            form={"avatar": cast(Any, (b"png-bytes", "image/png"))},
            into=UploadResult,
        )


def test_sync_post_with_form_mixed_kinds_in_repeated_tuple_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Mixed form value kinds for 'avatar'"):
        sync_client.post(
            "upload",
            form={"avatar": cast(Any, (b"png-bytes", "image/png"))},
            into=UploadResult,
        )


async def test_post_with_form_list_value_is_json_encoded_never_a_file(
    client: HTTPClient,
) -> None:
    # A list always means "JSON-encode me as one part" — ["name.txt", b"..."] must not be read as
    # a (filename, content) file just because a match sequence pattern also matches a list. The
    # stdlib encoder's own error is the expected one here, exactly as for any other bad JSON body.
    with pytest.raises(TypeError, match="not JSON serializable"):
        await client.post(
            "upload",
            form={"doc": ["doc.txt", b"file-content"]},
            into=UploadResult,
        )


def test_sync_post_with_form_list_value_is_json_encoded_never_a_file(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        sync_client.post(
            "upload",
            form={"doc": ["doc.txt", b"file-content"]},
            into=UploadResult,
        )


async def test_post_with_empty_form_repeat_tuple_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="Empty form value tuple for 'photos'"):
        await client.post("upload", form={"photos": ()}, into=UploadResult)


def test_sync_post_with_empty_form_repeat_tuple_raises_value_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(ValueError, match="Empty form value tuple for 'photos'"):
        sync_client.post("upload", form={"photos": ()}, into=UploadResult)


async def test_post_with_form_repeated_json_values(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"objects": ({"a": 1}, {"b": 2}), "arrays": (["a"], ["b"])},
            into=UploadResult,
        )
    ).data

    parts = result["parts"]
    objects = [part for part in parts if part["name"] == "objects"]
    arrays = [part for part in parts if part["name"] == "arrays"]
    assert [json.loads(part["text"]) for part in objects] == [{"a": 1}, {"b": 2}]
    assert [json.loads(part["text"]) for part in arrays] == [["a"], ["b"]]
    assert {part["content_type"] for part in objects + arrays} == {"application/json"}


@pytest.mark.parametrize(
    "body",
    [ItemModel(id=1, name="ditto"), ItemStruct(id=1, name="ditto")],
    ids=["pydantic", "msgspec"],
)
async def test_json_model_body_is_encoded_compactly_with_one_content_type(
    client: HTTPClient, body: ItemModel | ItemStruct
) -> None:
    result = (await client.post("echo-body", json=body, into=dict)).data

    assert result["body"] == '{"id":1,"name":"ditto"}'
    assert result["content_types"] == ["application/json"]


@pytest.mark.parametrize(
    "body",
    [ItemModel(id=1, name="ditto"), ItemStruct(id=1, name="ditto"), {"id": 1}],
    ids=["pydantic", "msgspec", "dict"],
)
async def test_json_body_replaces_a_callers_own_content_type_rather_than_duplicating_it(
    client: HTTPClient, body: ItemModel | ItemStruct | dict[str, int]
) -> None:
    # `.header()` would append a second Content-Type; the body setter must replace the caller's,
    # as `.body_json()` always has.
    result = (
        await client.post("echo-body", json=body, headers={"content-type": "text/plain"}, into=dict)
    ).data

    assert result["content_types"] == ["application/json"]


def test_sync_json_model_body_has_one_content_type(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "echo-body",
        json=ItemModel(id=1, name="ditto"),
        headers={"content-type": "text/plain"},
        into=dict,
    ).data

    assert result["content_types"] == ["application/json"]


async def test_post_with_a_top_level_json_array_body(client: HTTPClient) -> None:
    result = (await client.post("echo-body", json=[{"id": 1}, {"id": 2}], into=dict)).data

    assert json.loads(result["body"]) == [{"id": 1}, {"id": 2}]
    assert result["content_types"] == ["application/json"]


async def test_post_form_encodes_bools_like_params_and_accepts_floats(client: HTTPClient) -> None:
    # A bool used to hit the `int` branch and go out as Python's "True"; `params=` sends "true".
    result = (
        await client.post(
            "upload",
            form={"enabled": True, "disabled": False, "ratio": 1.5, "flags": (True, 0)},
            into=UploadResult,
        )
    ).data

    parts = result["parts"]
    assert {part["name"]: part["text"] for part in parts if part["name"] != "flags"} == {
        "enabled": "true",
        "disabled": "false",
        "ratio": "1.5",
    }
    assert [part["text"] for part in parts if part["name"] == "flags"] == ["true", "0"]


def test_sync_post_form_encodes_bools_like_params(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"enabled": True}, into=UploadResult).data

    assert result["fields"] == {"enabled": "true"}


@pytest.mark.parametrize(
    "body", [AliasedModel(userId=1), AliasedStruct(user_id=1)], ids=["pydantic", "msgspec"]
)
async def test_json_body_encodes_aliases_the_same_way_for_both_libraries(
    client: HTTPClient, body: AliasedModel | AliasedStruct
) -> None:
    # pydantic's own default serializes by field name; lothc encodes by alias like msgspec does,
    # so a camelCase model goes back to the API in camelCase.
    result = (await client.post("echo-body", json=body, into=dict)).data

    assert json.loads(result["body"]) == {"userId": 1}


async def test_an_explicit_serialize_by_alias_false_is_respected(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", json=ExplicitFieldNamesModel(userId=1), into=dict)
    ).data

    assert json.loads(result["body"]) == {"user_id": 1}


async def test_params_encode_aliases_too(client: HTTPClient) -> None:
    query = (await client.get("echo-query", params=AliasedModel(userId=1), into=dict)).data

    assert query["query"] == {"userId": ["1"]}


async def test_headers_encode_aliases_too(client: HTTPClient) -> None:
    echoed = (
        await client.get(
            "echo-headers",
            headers=AliasedHeaders(**{"X-Trace-Id": "trace"}),
            into=dict,
        )
    ).data

    assert echoed_headers(echoed)["x-trace-id"] == "trace"


async def test_form_parts_encode_aliases_too(client: HTTPClient) -> None:
    upload = (
        await client.post("upload", form={"meta": AliasedModel(userId=1)}, into=UploadResult)
    ).data

    assert json.loads(upload["fields"]["meta"]) == {"userId": 1}


def test_sync_json_body_encodes_aliases(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", json=AliasedModel(userId=1), into=dict).data

    assert json.loads(result["body"]) == {"userId": 1}


async def test_post_with_data_sends_a_urlencoded_body(client: HTTPClient) -> None:
    result = (await client.post("echo-body", data={"name": "a b&c", "count": 2}, into=dict)).data

    assert result["body"] == "name=a+b%26c&count=2"
    assert result["content_types"] == ["application/x-www-form-urlencoded"]


def test_sync_post_with_data_sends_a_urlencoded_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", data={"name": "a b&c", "count": 2}, into=dict).data

    assert result["body"] == "name=a+b%26c&count=2"
    assert result["content_types"] == ["application/x-www-form-urlencoded"]


async def test_data_repeats_a_key_per_sequence_element_and_encodes_bools(
    client: HTTPClient,
) -> None:
    result = (
        await client.post("echo-body", data={"tag": ["a", "b"], "enabled": True}, into=dict)
    ).data

    assert result["body"] == "tag=a&tag=b&enabled=true"


async def test_data_writes_an_int_outside_64_bits_as_its_exact_digits(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", data={"over": 2**63, "under": -(2**63) - 1}, into=dict)
    ).data

    assert result["body"] == "over=9223372036854775808&under=-9223372036854775809"


def test_sync_data_writes_an_int_outside_64_bits_as_its_exact_digits(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.post(
        "echo-body", data={"over": 2**63, "under": -(2**63) - 1}, into=dict
    ).data

    assert result["body"] == "over=9223372036854775808&under=-9223372036854775809"


async def test_data_accepts_a_pydantic_model_encoded_by_alias(client: HTTPClient) -> None:
    result = (await client.post("echo-body", data=AliasedModel(userId=1), into=dict)).data

    assert result["body"] == "userId=1"


async def test_data_accepts_a_msgspec_struct(client: HTTPClient) -> None:
    result = (await client.post("echo-body", data=ItemStruct(id=1, name="ditto"), into=dict)).data

    assert result["body"] == "id=1&name=ditto"


async def test_response_post_accepts_data(client: HTTPClient) -> None:
    result = await client.post("echo-body", data={"name": "ditto"}, into=dict)

    assert result.data["body"] == "name=ditto"


async def test_stream_post_accepts_data(client: HTTPClient) -> None:
    chunks = [chunk async for chunk in client.stream_post("echo-body", data={"name": "ditto"})]

    assert json.loads(b"".join(chunks))["body"] == "name=ditto"


def test_sync_stream_post_accepts_data(sync_client: SyncHTTPClient) -> None:
    chunks = list(sync_client.stream_post("echo-body", data={"name": "ditto"}))

    assert json.loads(b"".join(chunks))["body"] == "name=ditto"


async def test_data_and_form_together_are_rejected(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one of 'json', 'data', 'form' or 'content'"):
        await client.post("echo-body", data={"a": "1"}, form={"b": "2"})


def test_sync_data_and_json_together_are_rejected(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one of 'json', 'data', 'form' or 'content'"):
        sync_client.post("echo-body", data={"a": "1"}, json={"b": "2"})


async def test_delete_can_send_a_json_body(client: HTTPClient) -> None:
    result = (await client.delete("echo-body", json={"ids": [1, 2]}, into=dict)).data

    assert json.loads(result["body"]) == {"ids": [1, 2]}
    assert result["content_type"] == "application/json"


def test_sync_delete_can_send_a_json_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("echo-body", json={"ids": [1, 2]}, into=dict).data

    assert json.loads(result["body"]) == {"ids": [1, 2]}


async def test_response_delete_can_send_a_urlencoded_body(client: HTTPClient) -> None:
    result = await client.delete("echo-body", data={"id": "1"}, into=dict)

    assert result.data["body"] == "id=1"


def test_sync_response_delete_can_send_a_raw_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("echo-body", content="raw", into=dict)

    assert result.data["body"] == "raw"


@pytest.mark.parametrize("content", ["# Title", b"# Title"], ids=["str", "bytes"])
async def test_content_is_sent_with_exactly_the_callers_content_type(
    client: HTTPClient, content: str | bytes
) -> None:
    result = (
        await client.post(
            "echo-body", content=content, headers={"content-type": "text/markdown"}, into=dict
        )
    ).data

    assert result["body"] == "# Title"
    assert result["content_types"] == ["text/markdown"]


@pytest.mark.parametrize("content", ["# Title", b"# Title"], ids=["str", "bytes"])
def test_sync_content_is_sent_with_exactly_the_callers_content_type(
    sync_client: SyncHTTPClient, content: str | bytes
) -> None:
    result = sync_client.post(
        "echo-body", content=content, headers={"content-type": "text/markdown"}, into=dict
    ).data

    assert result["body"] == "# Title"
    assert result["content_types"] == ["text/markdown"]


class _OptionalFieldModel(BaseModel):
    a: int
    b: str | None = None


class _OptionalFieldStruct(Struct):
    a: int
    b: str | None = None


@pytest.mark.parametrize(
    "data", [_OptionalFieldModel(a=1), _OptionalFieldStruct(a=1)], ids=["pydantic", "msgspec"]
)
async def test_data_omits_a_models_none_fields(
    client: HTTPClient, data: _OptionalFieldModel | _OptionalFieldStruct
) -> None:
    result = (await client.post("echo-body", data=data, into=dict)).data

    assert result["body"] == "a=1"


@pytest.mark.parametrize(
    "data", [_OptionalFieldModel(a=1), _OptionalFieldStruct(a=1)], ids=["pydantic", "msgspec"]
)
def test_sync_data_omits_a_models_none_fields(
    sync_client: SyncHTTPClient, data: _OptionalFieldModel | _OptionalFieldStruct
) -> None:
    result = sync_client.post("echo-body", data=data, into=dict).data

    assert result["body"] == "a=1"


# --- form= parts that can't be built or sent ------------------------------------------------------
# Raised where pyreqwest raises them: a file that can't be opened, a part type that isn't one, or a
# name or value that isn't UTF-8 text, while the form is built, before anything is sent; a file
# that opens but can't be read (a directory), while the body is being sent.


_unbuildable_parts = pytest.mark.parametrize(
    ("form", "error", "match"),
    [
        ({"f": ("n.txt", b"x", "not a type")}, ValueError, "mime parse error"),
        ({cast(Any, 1): "v"}, TypeError, "'int' object"),
        ({cast(Any, 1): b"v"}, TypeError, "'int' object"),
        ({"\udc80": "v"}, UnicodeEncodeError, "surrogates not allowed"),
        ({"n": "\udc80"}, UnicodeEncodeError, "surrogates not allowed"),
        ({"n": ("\udc80.txt", b"x")}, UnicodeEncodeError, "surrogates not allowed"),
        ({"n": ("n.txt", b"x", "\udc80")}, UnicodeEncodeError, "surrogates not allowed"),
    ],
    ids=[
        "invalid-content-type",
        "text-name-not-a-str",
        "bytes-name-not-a-str",
        "name-not-utf-8",
        "value-not-utf-8",
        "filename-not-utf-8",
        "content-type-not-utf-8",
    ],
)


@_unbuildable_parts
async def test_a_form_part_that_cant_be_built_raises_before_anything_is_sent(
    client: HTTPClient, server_key: str, form: Form, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        await client.post("status-then-ok", params={"key": server_key}, form=form)

    assert (await client.get("hits", params={"key": server_key}, into=dict)).data == {"hits": 0}


@_unbuildable_parts
def test_sync_a_form_part_that_cant_be_built_raises_before_anything_is_sent(
    sync_client: SyncHTTPClient, server_key: str, form: Form, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        sync_client.post("status-then-ok", params={"key": server_key}, form=form)

    assert sync_client.get("hits", params={"key": server_key}, into=dict).data == {"hits": 0}


async def test_a_missing_form_file_raises_before_anything_is_sent(
    client: HTTPClient, server_key: str, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        await client.post(
            "status-then-ok", params={"key": server_key}, form={"f": tmp_path / "missing.txt"}
        )

    assert (await client.get("hits", params={"key": server_key}, into=dict)).data == {"hits": 0}


def test_sync_a_missing_form_file_raises_before_anything_is_sent(
    sync_client: SyncHTTPClient, server_key: str, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        sync_client.post(
            "status-then-ok",
            params={"key": server_key},
            form={"f": ("n.txt", tmp_path / "missing.txt")},
        )

    assert sync_client.get("hits", params={"key": server_key}, into=dict).data == {"hits": 0}


# Root reads a file whatever its permissions say (Windows has no root, nor `os.geteuid`).
_skip_as_root = pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0, reason="root can read an unreadable file"
)


@_skip_as_root
async def test_an_unreadable_form_file_raises_before_anything_is_sent(
    client: HTTPClient, server_key: str, tmp_path: Path
) -> None:
    locked = tmp_path / "locked.txt"
    locked.write_bytes(b"locked")
    make_unreadable(locked)

    with pytest.raises(PermissionError):
        await client.post("status-then-ok", params={"key": server_key}, form={"f": locked})

    assert (await client.get("hits", params={"key": server_key}, into=dict)).data == {"hits": 0}


@_skip_as_root
def test_sync_an_unreadable_form_file_raises_before_anything_is_sent(
    sync_client: SyncHTTPClient, server_key: str, tmp_path: Path
) -> None:
    locked = tmp_path / "locked.txt"
    locked.write_bytes(b"locked")
    make_unreadable(locked)

    with pytest.raises(PermissionError):
        sync_client.post("status-then-ok", params={"key": server_key}, form={"f": locked})

    assert sync_client.get("hits", params={"key": server_key}, into=dict).data == {"hits": 0}


async def test_a_form_file_that_cant_be_read_fails_the_request_after_the_parts_before_it(
    client: HTTPClient, base_url: str, server_key: str, tmp_path: Path
) -> None:
    unreadable = tmp_path / "unreadable"

    with opens_but_fails_to_read(unreadable):
        seen = await post_failing_form(client, base_url, server_key, {"a": "b", "f": unreadable})

    body = seen["body"]
    assert 'name="a"\r\n\r\nb\r\n--' in body
    assert body.endswith(
        'name="f"; filename="unreadable"\r\nContent-Type: application/octet-stream\r\n\r\n'
    )


def test_sync_a_form_file_that_cant_be_read_fails_the_request_after_the_parts_before_it(
    sync_client: SyncHTTPClient, base_url: str, server_key: str, tmp_path: Path
) -> None:
    unreadable = tmp_path / "unreadable"

    with opens_but_fails_to_read(unreadable):
        seen = sync_post_failing_form(
            sync_client, base_url, server_key, {"a": "b", "f": unreadable}
        )

    body = seen["body"]
    assert 'name="a"\r\n\r\nb\r\n--' in body
    assert body.endswith(
        'name="f"; filename="unreadable"\r\nContent-Type: application/octet-stream\r\n\r\n'
    )


# A file part is read as it's sent, never held whole: uploading 32 MiB grows a fresh process's
# peak memory by a small fraction of that. A fresh interpreter, since a peak only ever rises and
# this one's already has. The file is sparse, so writing it is instant.
def _upload_peak_growth_script(*, use_async: bool) -> str:
    upload = (
        "async def _upload():\n"
        "    async with HTTPClient(base_url=base_url) as client:\n"
        "        await client.post('count-body', form={'warm': 'up'})\n"
        "        before = _peak()\n"
        "        response = await client.post('count-body', form={'f': path}, into=dict)\n"
        "    return response.data['received'], _peak() - before\n"
        "received, growth = asyncio.run(_upload())\n"
        if use_async
        else "with SyncHTTPClient(base_url=base_url) as client:\n"
        "    client.post('count-body', form={'warm': 'up'})\n"
        "    before = _peak()\n"
        "    response = client.post('count-body', form={'f': path}, into=dict)\n"
        "received, growth = response.data['received'], _peak() - before\n"
    )
    return (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from lothc import HTTPClient, SyncHTTPClient\n"
        "base_url, path = sys.argv[1], Path(sys.argv[2])\n"
        "def _peak():\n"
        "    if sys.platform == 'win32':\n"
        "        # The peak working set: PROCESS_MEMORY_COUNTERS (72 bytes on 64-bit) starts with\n"
        "        # two DWORDs, its size and a count, then PeakWorkingSetSize.\n"
        "        import ctypes\n"
        "        counters = (ctypes.c_size_t * 9)(72)\n"
        "        process = ctypes.c_void_p(-1)  # GetCurrentProcess()'s pseudo-handle\n"
        "        if not ctypes.windll.kernel32.K32GetProcessMemoryInfo(process, counters, 72):\n"
        "            raise ctypes.WinError()\n"
        "        return counters[1]\n"
        "    import resource\n"
        "    # Bytes on macOS, kilobytes on Linux.\n"
        "    scale = 1 if sys.platform == 'darwin' else 1024\n"
        "    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale\n"
        f"{upload}"
        "print(received, growth)\n"
    )


def _upload_peak_growth(base_url: str, tmp_path: Path, *, use_async: bool) -> tuple[int, int]:
    """How many bytes the server received for a 32 MiB file part, and how much the uploading
    process's peak memory grew while it was sent."""
    large = tmp_path / "large.bin"
    with large.open("wb") as file:
        file.truncate(32 << 20)
    script = _upload_peak_growth_script(use_async=use_async)
    result = subprocess.run(
        [sys.executable, "-c", script, base_url, str(large)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    received, growth = result.stdout.split()
    return int(received), int(growth)


def test_a_large_form_file_is_streamed_not_held_in_memory(base_url: str, tmp_path: Path) -> None:
    received, growth = _upload_peak_growth(base_url, tmp_path, use_async=True)

    assert received > 32 << 20
    assert growth < 8 << 20


def test_sync_a_large_form_file_is_streamed_not_held_in_memory(
    base_url: str, tmp_path: Path
) -> None:
    received, growth = _upload_peak_growth(base_url, tmp_path, use_async=False)

    assert received > 32 << 20
    assert growth < 8 << 20
