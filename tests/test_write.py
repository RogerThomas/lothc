import json
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from msgspec import Struct
from pydantic import BaseModel, ConfigDict, Field

from lothc import HTTPClient, SyncHTTPClient


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
    x_custom: str | None = None


async def test_post_with_pydantic_json_body(client: HTTPClient) -> None:
    item = (
        await client.post("items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel)
    ).data

    assert item == ItemModel(id=1, name="ditto")


async def test_post_with_msgspec_json_body(client: HTTPClient) -> None:
    item = (
        await client.post(
            "items", json=ItemStruct(id=1, name="ditto"), response_data_type=ItemStruct
        )
    ).data

    assert item == ItemStruct(id=1, name="ditto")


async def test_post_with_raw_dict_json_body(client: HTTPClient) -> None:
    item = (
        await client.post("items", json={"id": 1, "name": "ditto"}, response_data_type=ItemModel)
    ).data

    assert item == ItemModel(id=1, name="ditto")


async def test_post_with_form_fields_and_files(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"note": "hello", "avatar": b"raw-bytes", "doc": ("doc.txt", b"file-content")},
            response_data_type=dict,
        )
    ).data

    assert result["fields"] == {"note": "hello", "avatar": "raw-bytes"}
    assert result["files"] == [{"name": "doc", "filename": "doc.txt", "size": 12}]


async def test_post_with_form_path_file(client: HTTPClient, tmp_path: Path) -> None:
    upload_path = tmp_path / "upload.txt"
    upload_path.write_bytes(b"path-content")

    result = (await client.post("upload", form={"doc": upload_path}, response_data_type=dict)).data

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
                response_data_type=dict,
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
        await client.post(
            "upload", form={"blob": BytesIO(b"blob-content")}, response_data_type=dict
        )
    ).data

    assert result["fields"] == {"blob": "blob-content"}
    assert result["files"] == []


async def test_post_with_form_file_mime_auto_inferred(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload", form={"avatar": ("a.png", b"png-bytes")}, response_data_type=dict
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts == [
        {"name": "avatar", "filename": "a.png", "content_type": "image/png", "text": "png-bytes"}
    ]


async def test_post_with_form_file_explicit_mime_override(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"avatar": ("a.png", b"png-bytes", "image/webp")},
            response_data_type=dict,
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "image/webp"


async def test_post_with_form_file_mime_inference_disabled(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"avatar": ("a.png", b"png-bytes")},
            infer_mime_type_from_file_extension=False,
            response_data_type=dict,
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] is None


async def test_post_with_form_path_with_content_type_override(
    client: HTTPClient, tmp_path: Path
) -> None:
    upload_path = tmp_path / "upload.bin"
    upload_path.write_bytes(b"path-content")

    result = (
        await client.post(
            "upload", form={"doc": (upload_path, "application/pdf")}, response_data_type=dict
        )
    ).data

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

    result = (await client.post("upload", form={"doc": upload_path}, response_data_type=dict)).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_buffered_io_mime_auto_inferred(
    client: HTTPClient, tmp_path: Path
) -> None:
    file_path = tmp_path / "opened.pdf"
    file_path.write_bytes(b"opened-content")

    with file_path.open("rb") as opened_file:
        result = (
            await client.post("upload", form={"doc": opened_file}, response_data_type=dict)
        ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/pdf"


async def test_post_with_form_repeated_scalar_values(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"tag": (1, "x")}, response_data_type=dict)).data

    parts = cast(list[dict[str, Any]], result["parts"])
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


async def test_post_with_form_repeated_file_values(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"photos": (("a.png", b"1"), ("b.png", b"2"))},
            response_data_type=dict,
        )
    ).data

    files = cast(list[dict[str, Any]], result["files"])
    photo_files = [file for file in files if file["name"] == "photos"]
    assert {file["filename"] for file in photo_files} == {"a.png", "b.png"}


async def test_post_with_form_json_array_body(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"tags": ["a", "b"]}, response_data_type=dict)).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == ["a", "b"]


async def test_post_with_form_json_object_body(client: HTTPClient) -> None:
    result = (await client.post("upload", form={"meta": {"k": "v"}}, response_data_type=dict)).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"k": "v"}


async def test_post_with_form_json_body_from_pydantic_model(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload", form={"meta": ItemModel(id=1, name="ditto")}, response_data_type=dict
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"id": 1, "name": "ditto"}


async def test_post_with_form_json_body_from_msgspec_struct(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload", form={"meta": ItemStruct(id=1, name="ditto")}, response_data_type=dict
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "application/json"
    assert json.loads(cast(str, parts[0]["text"])) == {"id": 1, "name": "ditto"}


def test_sync_post_with_form_file_explicit_mime_override(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "upload",
        form={"avatar": ("a.png", b"png-bytes", "image/webp")},
        response_data_type=dict,
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert parts[0]["content_type"] == "image/webp"


def test_sync_post_with_form_repeated_scalar_values(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tag": (1, "x")}, response_data_type=dict).data

    parts = cast(list[dict[str, Any]], result["parts"])
    tag_parts = [part for part in parts if part["name"] == "tag"]
    assert [part["text"] for part in tag_parts] == ["1", "x"]


def test_sync_post_with_form_json_array_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"tags": ["a", "b"]}, response_data_type=dict).data

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
                "typed_path_doc": (path_file, "text/plain"),
                "opened_doc": opened_file,
                "blob": BytesIO(b"blob-content"),
                "meta": {"key": "value"},
            },
            response_data_type=dict,
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
    parts = {part["name"]: part for part in cast(list[dict[str, Any]], result["parts"])}
    # A (Path, content_type) pair keeps the path's own filename and overrides only the mime.
    assert parts["typed_path_doc"]["filename"] == "path-upload.txt"
    assert parts["typed_path_doc"]["content_type"] == "text/plain"
    assert parts["meta"]["content_type"] == "application/json"
    assert json.loads(cast(str, parts["meta"]["text"])) == {"key": "value"}


async def test_put_replaces_item(client: HTTPClient) -> None:
    item = (
        await client.put(
            "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
        )
    ).data

    assert item == ItemModel(id=7, name="replaced")


async def test_patch_renames_item(client: HTTPClient) -> None:
    item = (
        await client.patch("items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel)
    ).data

    assert item == ItemModel(id=7, name="renamed")


async def test_response_post_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.post(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_response_post_with_typed_headers(client: HTTPClient) -> None:
    result = await client.post(
        "items",
        json=ItemModel(id=1, name="ditto"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_post_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=1, name="ditto")


async def test_response_post_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.post("missing", error_for_status=False)

    assert result.status == 404


async def test_response_put_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.put(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_response_put_with_typed_headers(client: HTTPClient) -> None:
    result = await client.put(
        "items/7",
        json=ItemModel(id=0, name="replaced"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_put_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.put(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="replaced")


async def test_response_patch_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.patch(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


async def test_response_patch_with_typed_headers(client: HTTPClient) -> None:
    result = await client.patch(
        "items/7",
        json=RenameBody(name="renamed"),
        response_data_type=ItemModel,
        response_headers_type=EchoedHeaders,
    )

    assert result.typed_headers is not None


def test_sync_patch_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.patch(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    )

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="renamed")


def test_sync_post_with_pydantic_json_body(sync_client: SyncHTTPClient) -> None:
    item = sync_client.post(
        "items", json=ItemModel(id=1, name="ditto"), response_data_type=ItemModel
    ).data

    assert item == ItemModel(id=1, name="ditto")


def test_sync_put_replaces_item(sync_client: SyncHTTPClient) -> None:
    item = sync_client.put(
        "items/7", json=ItemModel(id=0, name="replaced"), response_data_type=ItemModel
    ).data

    assert item == ItemModel(id=7, name="replaced")


def test_sync_patch_renames_item(sync_client: SyncHTTPClient) -> None:
    item = sync_client.patch(
        "items/7", json=RenameBody(name="renamed"), response_data_type=ItemModel
    ).data

    assert item == ItemModel(id=7, name="renamed")


async def test_post_with_string_content(client: HTTPClient) -> None:
    result = (await client.post("echo-body", content="hello", response_data_type=dict)).data

    assert result["body"] == "hello"


async def test_post_with_bytes_content(client: HTTPClient) -> None:
    result = (await client.post("echo-body", content=b"hello-bytes", response_data_type=dict)).data

    assert result["body"] == "hello-bytes"


async def test_post_with_more_than_one_body_kind_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="at most one"):
        await client.post("items", json={"id": 1}, content="also-content")


def test_sync_post_with_string_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content="hello", response_data_type=dict).data

    assert result["body"] == "hello"


def test_sync_post_with_bytes_content(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("echo-body", content=b"hello-bytes", response_data_type=dict).data

    assert result["body"] == "hello-bytes"


def test_sync_post_with_msgspec_json_body(sync_client: SyncHTTPClient) -> None:
    item = sync_client.post(
        "items", json=ItemStruct(id=1, name="ditto"), response_data_type=ItemModel
    ).data

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
            response_data_type=dict,
        )


def test_sync_post_with_form_mixed_kinds_in_repeated_tuple_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Mixed form value kinds for 'avatar'"):
        sync_client.post(
            "upload",
            form={"avatar": cast(Any, (b"png-bytes", "image/png"))},
            response_data_type=dict,
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
            response_data_type=dict,
        )


def test_sync_post_with_form_list_value_is_json_encoded_never_a_file(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        sync_client.post(
            "upload",
            form={"doc": ["doc.txt", b"file-content"]},
            response_data_type=dict,
        )


async def test_post_with_empty_form_repeat_tuple_raises_value_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="Empty form value tuple for 'photos'"):
        await client.post("upload", form={"photos": ()}, response_data_type=dict)


def test_sync_post_with_empty_form_repeat_tuple_raises_value_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(ValueError, match="Empty form value tuple for 'photos'"):
        sync_client.post("upload", form={"photos": ()}, response_data_type=dict)


async def test_post_with_form_repeated_json_values(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload",
            form={"objects": ({"a": 1}, {"b": 2}), "arrays": (["a"], ["b"])},
            response_data_type=dict,
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    objects = [part for part in parts if part["name"] == "objects"]
    arrays = [part for part in parts if part["name"] == "arrays"]
    assert [json.loads(cast(str, part["text"])) for part in objects] == [{"a": 1}, {"b": 2}]
    assert [json.loads(cast(str, part["text"])) for part in arrays] == [["a"], ["b"]]
    assert {part["content_type"] for part in objects + arrays} == {"application/json"}


@pytest.mark.parametrize(
    "body",
    [ItemModel(id=1, name="ditto"), ItemStruct(id=1, name="ditto")],
    ids=["pydantic", "msgspec"],
)
async def test_json_model_body_is_encoded_compactly_with_one_content_type(
    client: HTTPClient, body: ItemModel | ItemStruct
) -> None:
    result = (await client.post("echo-body", json=body, response_data_type=dict)).data

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
        await client.post(
            "echo-body", json=body, headers={"content-type": "text/plain"}, response_data_type=dict
        )
    ).data

    assert result["content_types"] == ["application/json"]


def test_sync_json_model_body_has_one_content_type(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "echo-body",
        json=ItemModel(id=1, name="ditto"),
        headers={"content-type": "text/plain"},
        response_data_type=dict,
    ).data

    assert result["content_types"] == ["application/json"]


async def test_post_with_a_top_level_json_array_body(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", json=[{"id": 1}, {"id": 2}], response_data_type=dict)
    ).data

    assert json.loads(result["body"]) == [{"id": 1}, {"id": 2}]
    assert result["content_types"] == ["application/json"]


async def test_post_form_encodes_bools_like_params_and_accepts_floats(client: HTTPClient) -> None:
    # A bool used to hit the `int` branch and go out as Python's "True"; `params=` sends "true".
    result = (
        await client.post(
            "upload",
            form={"enabled": True, "disabled": False, "ratio": 1.5, "flags": (True, 0)},
            response_data_type=dict,
        )
    ).data

    parts = cast(list[dict[str, Any]], result["parts"])
    assert {part["name"]: part["text"] for part in parts if part["name"] != "flags"} == {
        "enabled": "true",
        "disabled": "false",
        "ratio": "1.5",
    }
    assert [part["text"] for part in parts if part["name"] == "flags"] == ["true", "0"]


def test_sync_post_form_encodes_bools_like_params(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("upload", form={"enabled": True}, response_data_type=dict).data

    assert result["fields"] == {"enabled": "true"}


@pytest.mark.parametrize(
    "body", [AliasedModel(userId=1), AliasedStruct(user_id=1)], ids=["pydantic", "msgspec"]
)
async def test_json_body_encodes_aliases_the_same_way_for_both_libraries(
    client: HTTPClient, body: AliasedModel | AliasedStruct
) -> None:
    # pydantic's own default serializes by field name; lothc encodes by alias like msgspec does,
    # so a camelCase model goes back to the API in camelCase.
    result = (await client.post("echo-body", json=body, response_data_type=dict)).data

    assert json.loads(result["body"]) == {"userId": 1}


async def test_an_explicit_serialize_by_alias_false_is_respected(client: HTTPClient) -> None:
    result = (
        await client.post(
            "echo-body", json=ExplicitFieldNamesModel(userId=1), response_data_type=dict
        )
    ).data

    assert json.loads(result["body"]) == {"user_id": 1}


async def test_params_headers_and_form_parts_encode_aliases_too(client: HTTPClient) -> None:
    query = (
        await client.get("echo-query", params=AliasedModel(userId=1), response_data_type=dict)
    ).data
    echoed = (
        await client.get(
            "echo-headers",
            headers=AliasedHeaders(**{"X-Trace-Id": "trace"}),
            response_data_type=dict,
        )
    ).data
    upload = (
        await client.post("upload", form={"meta": AliasedModel(userId=1)}, response_data_type=dict)
    ).data

    assert query["query"] == {"userId": ["1"]}
    assert {h["name"].lower(): h["value"] for h in echoed["headers"]}["x-trace-id"] == "trace"
    assert json.loads(cast(str, upload["fields"]["meta"])) == {"userId": 1}


def test_sync_json_body_encodes_aliases(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "echo-body", json=AliasedModel(userId=1), response_data_type=dict
    ).data

    assert json.loads(result["body"]) == {"userId": 1}


async def test_post_with_data_sends_a_urlencoded_body(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", data={"name": "a b&c", "count": 2}, response_data_type=dict)
    ).data

    assert result["body"] == "name=a+b%26c&count=2"
    assert result["content_types"] == ["application/x-www-form-urlencoded"]


def test_sync_post_with_data_sends_a_urlencoded_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post(
        "echo-body", data={"name": "a b&c", "count": 2}, response_data_type=dict
    ).data

    assert result["body"] == "name=a+b%26c&count=2"
    assert result["content_types"] == ["application/x-www-form-urlencoded"]


async def test_data_repeats_a_key_per_sequence_element_and_encodes_bools(
    client: HTTPClient,
) -> None:
    result = (
        await client.post(
            "echo-body", data={"tag": ["a", "b"], "enabled": True}, response_data_type=dict
        )
    ).data

    assert result["body"] == "tag=a&tag=b&enabled=true"


async def test_data_accepts_a_pydantic_model_encoded_by_alias(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", data=AliasedModel(userId=1), response_data_type=dict)
    ).data

    assert result["body"] == "userId=1"


async def test_data_accepts_a_msgspec_struct(client: HTTPClient) -> None:
    result = (
        await client.post("echo-body", data=ItemStruct(id=1, name="ditto"), response_data_type=dict)
    ).data

    assert result["body"] == "id=1&name=ditto"


async def test_response_post_accepts_data(client: HTTPClient) -> None:
    result = await client.post("echo-body", data={"name": "ditto"}, response_data_type=dict)

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
    result = (await client.delete("echo-body", json={"ids": [1, 2]}, response_data_type=dict)).data

    assert json.loads(result["body"]) == {"ids": [1, 2]}
    assert result["content_type"] == "application/json"


def test_sync_delete_can_send_a_json_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("echo-body", json={"ids": [1, 2]}, response_data_type=dict).data

    assert json.loads(result["body"]) == {"ids": [1, 2]}


async def test_response_delete_can_send_a_urlencoded_body(client: HTTPClient) -> None:
    result = await client.delete("echo-body", data={"id": "1"}, response_data_type=dict)

    assert result.data["body"] == "id=1"


def test_sync_response_delete_can_send_a_raw_body(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("echo-body", content="raw", response_data_type=dict)

    assert result.data["body"] == "raw"


async def test_delete_without_a_body_still_works(client: HTTPClient) -> None:
    result = (await client.delete("items/7", response_data_type=dict)).data

    assert result == {"id": 7, "deleted": True}
