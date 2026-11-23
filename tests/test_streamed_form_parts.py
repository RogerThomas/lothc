"""A `form=` file part streamed from an iterator, or on `HTTPClient` an async iterable: pulled a
chunk at a time as the body is sent, never held whole, with the whole body then sent chunked (no
`Content-Length`), and the request sent once."""

import asyncio
import time
from collections.abc import AsyncIterator, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from lothc import HTTPClient, HTTPResponseError, RequestHook, RequestInfo, SyncHTTPClient
from lothc.testing import LOTHCMocker


def _chunks(*chunks: bytes) -> Iterator[bytes]:
    yield from chunks


async def _async_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


async def _async_texts() -> AsyncIterator[Any]:
    yield "one"


def _failing_chunks() -> Iterator[bytes]:
    yield b"one"
    raise RuntimeError("chunk-error")


async def _async_failing_chunks() -> AsyncIterator[bytes]:
    yield b"one"
    await asyncio.sleep(0)
    raise RuntimeError("chunk-error")


@dataclass(slots=True)
class _AsyncChunks:
    """An async iterable that isn't itself an iterator: each `async for` gets a fresh one."""

    chunks: tuple[bytes, ...]

    def __aiter__(self) -> AsyncIterator[bytes]:
        return _async_chunks(*self.chunks)


@dataclass(slots=True)
class _Pulls:
    """`count` copies of `chunk`, streamed (`stream()`), recording when each was pulled."""

    chunk: bytes
    count: int
    pulled_at: list[float] = field(default_factory=list[float])

    def stream(self) -> Iterator[bytes]:
        for _ in range(self.count):
            self.pulled_at.append(time.monotonic())
            yield self.chunk

    async def async_stream(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            self.pulled_at.append(time.monotonic())
            yield self.chunk


@dataclass(slots=True)
class _StatusRecorder:
    """A `request_hook=` that records the status each attempt ended with."""

    statuses: list[int | None] = field(default_factory=list[int | None])

    @contextmanager
    def __call__(self, _request: RequestInfo) -> Generator[RequestHook]:
        hook = RequestHook()
        try:
            yield hook
        finally:
            self.statuses.append(hook.status)


class _Tokens:
    """A `bearer_auth` that can `invalidate()`, so a 401 re-authenticates (a plain class: zuban
    rejects a dataclass as `bearer_auth=`)."""

    def __init__(self) -> None:
        self.invalidated: list[str | None] = []

    async def __call__(self) -> str:
        return "token"

    def invalidate(self, stale_access_token: str | None = None) -> None:
        self.invalidated.append(stale_access_token)


class _SyncTokens:
    def __init__(self) -> None:
        self.invalidated: list[str | None] = []

    def __call__(self) -> str:
        return "token"

    def invalidate(self, stale_access_token: str | None = None) -> None:
        self.invalidated.append(stale_access_token)


def _echoed_headers(echoed: dict[str, Any]) -> dict[str, str]:
    """`/echo-request`'s headers by name, as they arrived."""
    return dict(echoed["headers"])


# --- Framing ------------------------------------------------------------------------------------


async def test_a_form_with_a_streamed_part_is_sent_chunked(client: HTTPClient) -> None:
    echoed = (
        await client.post(
            "echo-request", form={"a": "b", "f": ("f.bin", _chunks(b"one", b"two"))}, into=dict
        )
    ).data

    headers = _echoed_headers(echoed)
    assert headers["Transfer-Encoding"] == "chunked"
    assert "Content-Length" not in headers


def test_sync_a_form_with_a_streamed_part_is_sent_chunked(sync_client: SyncHTTPClient) -> None:
    echoed = sync_client.post(
        "echo-request", form={"a": "b", "f": ("f.bin", _chunks(b"one", b"two"))}, into=dict
    ).data

    headers = _echoed_headers(echoed)
    assert headers["Transfer-Encoding"] == "chunked"
    assert "Content-Length" not in headers


async def test_a_form_without_a_streamed_part_keeps_its_content_length(client: HTTPClient) -> None:
    echoed = (
        await client.post("echo-request", form={"a": "b", "f": ("f.bin", b"one")}, into=dict)
    ).data

    headers = _echoed_headers(echoed)
    assert "Transfer-Encoding" not in headers
    assert int(headers["Content-Length"]) == len(echoed["body"])


def test_sync_a_form_without_a_streamed_part_keeps_its_content_length(
    sync_client: SyncHTTPClient,
) -> None:
    echoed = sync_client.post(
        "echo-request", form={"a": "b", "f": ("f.bin", b"one")}, into=dict
    ).data

    headers = _echoed_headers(echoed)
    assert "Transfer-Encoding" not in headers
    assert int(headers["Content-Length"]) == len(echoed["body"])


# --- What arrives ------------------------------------------------------------------------------


async def test_streamed_parts_arrive_whole_with_their_filenames_and_types(
    client: HTTPClient,
) -> None:
    result = (
        await client.post(
            "upload",
            form={
                "note": "n",
                "f": ("f.txt", _chunks(b"one", b"", b"two")),
                "g": ("g.bin", _chunks(b"x"), "application/x-g"),
                "empty": ("e.bin", _chunks()),
                "r": (("a.txt", _chunks(b"1")), ("b.txt", _chunks(b"2"))),
            },
            into=dict,
        )
    ).data

    assert result["parts"] == [
        {"name": "note", "filename": None, "content_type": None, "text": "n"},
        {"name": "f", "filename": "f.txt", "content_type": "text/plain", "text": "onetwo"},
        {"name": "g", "filename": "g.bin", "content_type": "application/x-g", "text": "x"},
        {
            "name": "empty",
            "filename": "e.bin",
            "content_type": "application/octet-stream",
            "text": "",
        },
        {"name": "r", "filename": "a.txt", "content_type": "text/plain", "text": "1"},
        {"name": "r", "filename": "b.txt", "content_type": "text/plain", "text": "2"},
    ]


def test_sync_streamed_parts_arrive_whole_with_their_filenames_and_types(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.post(
        "upload",
        form={
            "note": "n",
            "f": ("f.txt", _chunks(b"one", b"", b"two")),
            "g": ("g.bin", _chunks(b"x"), "application/x-g"),
            "empty": ("e.bin", _chunks()),
            "r": (("a.txt", _chunks(b"1")), ("b.txt", _chunks(b"2"))),
        },
        into=dict,
    ).data

    assert result["parts"] == [
        {"name": "note", "filename": None, "content_type": None, "text": "n"},
        {"name": "f", "filename": "f.txt", "content_type": "text/plain", "text": "onetwo"},
        {"name": "g", "filename": "g.bin", "content_type": "application/x-g", "text": "x"},
        {
            "name": "empty",
            "filename": "e.bin",
            "content_type": "application/octet-stream",
            "text": "",
        },
        {"name": "r", "filename": "a.txt", "content_type": "text/plain", "text": "1"},
        {"name": "r", "filename": "b.txt", "content_type": "text/plain", "text": "2"},
    ]


async def test_a_streamed_part_s_type_is_not_guessed_when_inference_is_off(
    client: HTTPClient,
) -> None:
    result = (
        await client.post(
            "upload",
            form={"f": ("f.txt", _chunks(b"one"))},
            infer_mime_type_from_file_extension=False,
            into=dict,
        )
    ).data

    assert result["parts"] == [
        {"name": "f", "filename": "f.txt", "content_type": None, "text": "one"}
    ]


def test_sync_a_streamed_part_s_type_is_not_guessed_when_inference_is_off(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.post(
        "upload",
        form={"f": ("f.txt", _chunks(b"one"))},
        infer_mime_type_from_file_extension=False,
        into=dict,
    ).data

    assert result["parts"] == [
        {"name": "f", "filename": "f.txt", "content_type": None, "text": "one"}
    ]


async def test_a_file_part_streams_from_an_async_generator(client: HTTPClient) -> None:
    result = (
        await client.post("upload", form={"f": ("f.txt", _async_chunks(b"one", b"two"))}, into=dict)
    ).data

    assert result["parts"] == [
        {"name": "f", "filename": "f.txt", "content_type": "text/plain", "text": "onetwo"}
    ]


async def test_a_file_part_streams_from_an_async_iterable(client: HTTPClient) -> None:
    result = (
        await client.post(
            "upload", form={"f": ("f.bin", _AsyncChunks((b"one", b"two")), "x/y")}, into=dict
        )
    ).data

    assert result["parts"] == [
        {"name": "f", "filename": "f.bin", "content_type": "x/y", "text": "onetwo"}
    ]


def test_sync_an_async_iterable_is_refused_before_anything_is_sent(
    sync_client: SyncHTTPClient, server_key: str
) -> None:
    with pytest.raises(TypeError, match="not an async iterable"):
        sync_client.post(
            "status-then-ok",
            params={"key": server_key},
            form={"a": "b", "f": ("f.bin", _AsyncChunks((b"one",)))},
        )
    hits = sync_client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 0}


# In a form, a `list` is one JSON part and a `tuple` a repeat, so neither is ever a stream: a file
# tuple holding one is a repeat of two kinds of value.
async def test_a_list_is_never_a_stream(client: HTTPClient) -> None:
    value: Any = ("f.bin", [b"one"])

    with pytest.raises(TypeError, match="Mixed form value kinds for 'f'"):
        await client.post("upload", form={"f": value})


def test_sync_a_list_is_never_a_stream(sync_client: SyncHTTPClient) -> None:
    value: Any = ("f.bin", [b"one"])

    with pytest.raises(TypeError, match="Mixed form value kinds for 'f'"):
        sync_client.post("upload", form={"f": value})


# --- Pulled as it's sent ------------------------------------------------------------------------
# 24 chunks of 1 MiB: by the time the last is pulled, the server already has 4 MiB of the body,
# more than every buffer between them (hyper's, both sockets') could hold of what was pulled before
# it, so nothing pulls the stream whole before sending it.


async def test_a_streamed_part_is_sent_as_it_is_pulled(client: HTTPClient) -> None:
    pulls = _Pulls(bytes(1 << 20), 24)

    result = (
        await client.post(
            "body-arrival",
            params={"after": 4 << 20},
            form={"f": ("f.bin", pulls.stream())},
            into=dict,
        )
    ).data

    assert result["size"] > 24 << 20
    assert result["reached_at"] < pulls.pulled_at[-1]


def test_sync_a_streamed_part_is_sent_as_it_is_pulled(sync_client: SyncHTTPClient) -> None:
    pulls = _Pulls(bytes(1 << 20), 24)

    result = sync_client.post(
        "body-arrival",
        params={"after": 4 << 20},
        form={"f": ("f.bin", pulls.stream())},
        into=dict,
    ).data

    assert result["size"] > 24 << 20
    assert result["reached_at"] < pulls.pulled_at[-1]


async def test_an_async_streamed_part_is_sent_as_it_is_pulled(client: HTTPClient) -> None:
    pulls = _Pulls(bytes(1 << 20), 24)

    result = (
        await client.post(
            "body-arrival",
            params={"after": 4 << 20},
            form={"f": ("f.bin", pulls.async_stream())},
            into=dict,
        )
    ).data

    assert result["size"] > 24 << 20
    assert result["reached_at"] < pulls.pulled_at[-1]


# --- Failures -----------------------------------------------------------------------------------


async def test_an_exception_a_streamed_part_raises_is_raised_as_it_was(
    client: HTTPClient,
) -> None:
    with pytest.raises(RuntimeError, match="chunk-error"):
        await client.post("upload", form={"a": "b", "f": ("f.bin", _failing_chunks())})
    after = (await client.post("upload", form={"a": "b"}, into=dict)).data

    assert after["fields"] == {"a": "b"}


def test_sync_an_exception_a_streamed_part_raises_is_raised_as_it_was(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(RuntimeError, match="chunk-error"):
        sync_client.post("upload", form={"a": "b", "f": ("f.bin", _failing_chunks())})
    after = sync_client.post("upload", form={"a": "b"}, into=dict).data

    assert after["fields"] == {"a": "b"}


async def test_an_exception_an_async_streamed_part_raises_is_raised_as_it_was(
    client: HTTPClient,
) -> None:
    with pytest.raises(RuntimeError, match="chunk-error"):
        await client.post("upload", form={"a": "b", "f": ("f.bin", _async_failing_chunks())})
    after = (await client.post("upload", form={"a": "b"}, into=dict)).data

    assert after["fields"] == {"a": "b"}


async def test_a_chunk_that_is_not_bytes_raises_type_error(client: HTTPClient) -> None:
    chunks: Iterator[Any] = iter([b"one", bytearray(b"two")])

    with pytest.raises(TypeError, match="'f' yielded a bytearray chunk"):
        await client.post("upload", form={"f": ("f.bin", chunks)})


def test_sync_a_chunk_that_is_not_bytes_raises_type_error(sync_client: SyncHTTPClient) -> None:
    chunks: Iterator[Any] = iter([b"one", bytearray(b"two")])

    with pytest.raises(TypeError, match="'f' yielded a bytearray chunk"):
        sync_client.post("upload", form={"f": ("f.bin", chunks)})


async def test_an_async_chunk_that_is_not_bytes_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="'f' yielded a str chunk"):
        await client.post("upload", form={"f": ("f.bin", _async_texts())})


# --- Sent once ----------------------------------------------------------------------------------


async def test_a_form_with_a_streamed_part_is_never_retried(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.put(
                "status-then-ok",
                params={"key": server_key, "fail_times": 1},
                form={"f": ("f.bin", _chunks(b"one"))},
            )
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 1}


def test_sync_a_form_with_a_streamed_part_is_never_retried(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.put(
                "status-then-ok",
                params={"key": server_key, "fail_times": 1},
                form={"f": ("f.bin", _chunks(b"one"))},
            )
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 1}


async def test_a_401_on_a_form_with_a_streamed_part_is_not_sent_again(
    base_url: str, server_key: str
) -> None:
    tokens = _Tokens()
    async with HTTPClient(base_url=base_url, bearer_auth=tokens) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.put(
                "status-then-ok",
                params={"key": server_key, "status": 401, "fail_times": 1},
                form={"f": ("f.bin", _chunks(b"one"))},
            )
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert exc_info.value.status == 401
    assert hits == {"hits": 1}
    assert tokens.invalidated == ["token"]


def test_sync_a_401_on_a_form_with_a_streamed_part_is_not_sent_again(
    base_url: str, server_key: str
) -> None:
    tokens = _SyncTokens()
    with SyncHTTPClient(base_url=base_url, bearer_auth=tokens) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.put(
                "status-then-ok",
                params={"key": server_key, "status": 401, "fail_times": 1},
                form={"f": ("f.bin", _chunks(b"one"))},
            )
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert exc_info.value.status == 401
    assert hits == {"hits": 1}
    assert tokens.invalidated == ["token"]


# --- With a request hook, and mocked ------------------------------------------------------------


async def test_a_hooked_request_with_a_streamed_part_shows_the_hook_its_status(
    base_url: str,
) -> None:
    recorder = _StatusRecorder()
    async with HTTPClient(base_url=base_url, request_hook=recorder) as client:
        result = (
            await client.post("upload", form={"f": ("f.txt", _chunks(b"one"))}, into=dict)
        ).data

    assert result["files"] == [{"name": "f", "filename": "f.txt", "size": 3}]
    assert recorder.statuses == [200]


def test_sync_a_hooked_request_with_a_streamed_part_shows_the_hook_its_status(
    base_url: str,
) -> None:
    recorder = _StatusRecorder()
    with SyncHTTPClient(base_url=base_url, request_hook=recorder) as client:
        result = client.post("upload", form={"f": ("f.txt", _chunks(b"one"))}, into=dict).data

    assert result["files"] == [{"name": "f", "filename": "f.txt", "size": 3}]
    assert recorder.statuses == [200]


# A mock answers before the transport sends the body (as pyreqwest's mock middleware does), so a
# streamed part is never pulled, and the mock sees no `Content-Length` and no body, as for any
# `form=` body.
async def test_a_mock_answers_a_streamed_part_without_pulling_it(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/upload", data=b"mocked")
    pulls = _Pulls(b"one", 1)

    response = await client.post("upload", form={"a": "b", "f": ("f.bin", pulls.stream())})

    [seen] = mock.get_requests()
    assert response.data == b"mocked"
    assert seen.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "content-length" not in seen.headers
    assert seen.body is None
    assert pulls.pulled_at == []


def test_sync_a_mock_answers_a_streamed_part_without_pulling_it(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/upload", data=b"mocked")
    pulls = _Pulls(b"one", 1)

    response = sync_client.post("upload", form={"a": "b", "f": ("f.bin", pulls.stream())})

    [seen] = mock.get_requests()
    assert response.data == b"mocked"
    assert seen.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert "content-length" not in seen.headers
    assert seen.body is None
    assert pulls.pulled_at == []
