import asyncio
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI()


@app.get("/slow")
async def slow() -> dict[str, bool]:
    await asyncio.sleep(3)
    return {"finally": True}


@app.get("/echo-headers")
async def echo_headers(request: Request) -> dict[str, str]:
    return dict(request.headers)


class Item(BaseModel):
    id: int
    name: str


class SearchResult(BaseModel):
    q: str
    page: int
    items: list[Item]


@app.get("/items")
async def search_items(q: str, page: int = 1) -> SearchResult:
    return SearchResult(q=q, page=page, items=[Item(id=page, name=f"{q}-match")])


@app.get("/items/{item_id}")
async def get_item(item_id: int) -> Item:
    return Item(id=item_id, name=f"item-{item_id}")


@app.post("/items")
async def create_item(item: Item) -> Item:
    return item


@app.put("/items/{item_id}")
async def replace_item(item_id: int, item: Item) -> Item:
    return Item(id=item_id, name=item.name)


@app.patch("/items/{item_id}")
async def rename_item(item_id: int, body: dict[str, str]) -> Item:
    return Item(id=item_id, name=body["name"])


async def _event_stream() -> AsyncGenerator[str]:
    for index in range(25):
        payload = f'{{"msg": "hello {index}", "now": {index * 100}}}'
        yield f"id: {index}\nevent: tick\ndata: {payload}\n\n"
        await asyncio.sleep(0)


@app.get("/events")
async def events() -> StreamingResponse:
    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@app.get("/boom")
async def boom() -> StreamingResponse:
    raise ValueError("simulated failure")
