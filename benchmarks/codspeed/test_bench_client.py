"""CodSpeed benchmarks for lothc's own per-request overhead.

Every request goes through `lothc_mocker`, so no socket is opened: what is measured is lothc's
request building (params/headers/body encoding), pyreqwest dispatch and response decoding into
each supported target, which is the CPU-bound work lothc adds on top of pyreqwest.
"""

import asyncio
import json
from typing import Any

import pytest
from msgspec import Struct
from pydantic import BaseModel, TypeAdapter

from lothc import CaseInsensitiveDict, HTTPClient, HTTPResponseError, SyncHTTPClient
from lothc.testing import LOTHCMocker


class ItemModel(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]


class ItemStruct(Struct):
    id: int
    name: str
    price: float
    tags: list[str]


class ErrorModel(BaseModel):
    code: str
    message: str


class ModelAPIError(HTTPResponseError[ErrorModel]):
    pass


class QueryModel(BaseModel):
    page: int
    per_page: int
    sort: str | None = None


ITEM: dict[str, Any] = {"id": 7, "name": "item-7", "price": 9.99, "tags": ["a", "b", "c"]}
ITEMS = [{**ITEM, "id": i, "name": f"item-{i}"} for i in range(500)]
ITEMS_BODY = json.dumps(ITEMS).encode()
NDJSON_BODY = b"".join(json.dumps(item).encode() + b"\n" for item in ITEMS)
SSE_BODY = b"".join(
    f"id: {i}\nevent: item\ndata: {json.dumps(item)}\n\n".encode() for i, item in enumerate(ITEMS)
)
ITEM_LIST_ADAPTER = TypeAdapter(list[ItemModel])


@pytest.mark.parametrize(
    "into",
    [bytes, dict, ItemModel, ItemStruct],
    ids=["bytes", "dict", "pydantic", "msgspec"],
)
def test_sync_get_small(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
    into: type[Any],
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ITEM)

    @benchmark
    def _() -> None:
        sync_client.get("items/7", into=into)


def test_sync_get_large_list_pydantic(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(path="/items", data=ITEMS_BODY)

    @benchmark
    def _() -> None:
        response = sync_client.get("items", into=ITEM_LIST_ADAPTER)
        assert len(response.data) == len(ITEMS)


def test_sync_get_with_params_and_headers(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(
        path="/items", data=ITEM, headers={"x-total-count": "500", "x-request-id": "abc"}
    )
    params = QueryModel(page=2, per_page=50, sort="name")
    headers = {"x-trace-id": "trace", "accept": "application/json"}

    @benchmark
    def _() -> None:
        response = sync_client.get("items", params=params, headers=headers, into=dict)
        assert response.headers["X-Total-Count"] == "500"


@pytest.mark.parametrize(
    "payload",
    [ITEM, ItemModel(**ITEM), ItemStruct(**ITEM)],
    ids=["dict", "pydantic", "msgspec"],
)
def test_sync_post_json(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
    payload: Any,  # noqa: ANN401
) -> None:
    lothc_mocker.add_post_response(path="/items", data=ITEM, status=201)

    @benchmark
    def _() -> None:
        sync_client.post("items", json=payload, into=ItemModel)


def test_sync_post_form(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_post_response(path="/upload", data=b"ok")
    form: Any = {
        "name": "item-7",
        "count": 3,
        "enabled": True,
        "tags": ("a", "b", "c"),
        "meta": ItemModel(**ITEM),
        "blob": b"\x00" * 1024,
    }

    @benchmark
    def _() -> None:
        sync_client.post("upload", form=form)


def test_sync_error_response(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(
        path="/missing", data={"code": "not_found", "message": "nope"}, status=404
    )

    @benchmark
    def _() -> None:
        with pytest.raises(ModelAPIError):
            sync_client.get("missing", raises=ModelAPIError)


def test_sync_stream_get_ndjson(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(path="/stream", data=NDJSON_BODY)

    @benchmark
    def _() -> None:
        count = sum(1 for _ in sync_client.stream_get("stream", into=ItemStruct))
        assert count == len(ITEMS)


def test_sync_sse(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(
        path="/events", data=SSE_BODY, headers={"content-type": "text/event-stream"}
    )

    @benchmark
    def _() -> None:
        count = sum(1 for _ in sync_client.sse("events", into=ItemModel))
        assert count == len(ITEMS)


def test_sync_download(
    benchmark: Any,  # noqa: ANN401
    sync_client: SyncHTTPClient,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(path="/file", data=b"x" * 1_000_000)

    @benchmark
    def _() -> None:
        body = sync_client.download("file")
        assert body is not None


@pytest.mark.parametrize(
    "into",
    [bytes, ItemModel, ItemStruct],
    ids=["bytes", "pydantic", "msgspec"],
)
def test_async_get_small(
    benchmark: Any,  # noqa: ANN401
    async_client: HTTPClient,
    event_loop_runner: asyncio.Runner,
    lothc_mocker: LOTHCMocker,
    into: type[Any],
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ITEM)

    @benchmark
    def _() -> None:
        event_loop_runner.run(async_client.get("items/7", into=into))


def test_async_gather_get(
    benchmark: Any,  # noqa: ANN401
    async_client: HTTPClient,
    event_loop_runner: asyncio.Runner,
    lothc_mocker: LOTHCMocker,
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ITEM)

    async def _many() -> None:
        await asyncio.gather(*(async_client.get("items/7", into=ItemStruct) for _ in range(50)))

    @benchmark
    def _() -> None:
        event_loop_runner.run(_many())


def test_case_insensitive_dict(benchmark: Any) -> None:  # noqa: ANN401
    pairs = [(f"X-Header-{i}", f"value-{i}") for i in range(50)]

    @benchmark
    def _() -> None:
        headers = CaseInsensitiveDict(pairs)
        for i in range(50):
            _ = headers[f"x-header-{i}"]
        headers.get_all("X-HEADER-0")
