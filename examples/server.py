import asyncio
from collections.abc import AsyncIterator

from jero import BaseApp, Endpoint, RawHeaders, Resource, ServerSentEvent, SSEResponse, Struct
from msgspec import field


class SlowResponse(Struct):
    finally_: bool = field(name="finally")


class Header(Struct):
    name: str
    value: str


class EchoHeaders(Struct):
    headers: list[Header]


class Item(Struct):
    id: int
    name: str


class ItemPath(Struct):
    item_id: int


class SearchParams(Struct):
    q: str
    page: int = 1


class SearchResult(Struct):
    q: str
    page: int
    items: list[Item]


class RenameItem(Struct):
    name: str


class TickEvent(Struct):
    msg: str
    now: int


class SlowEndpoint(Endpoint, path="/slow"):
    async def get(self) -> SlowResponse:
        await asyncio.sleep(3)
        return SlowResponse(finally_=True)


class EchoHeadersEndpoint(Endpoint, path="/echo-headers"):
    async def get(self, raw_headers: RawHeaders) -> EchoHeaders:
        return EchoHeaders(
            headers=[Header(name=name, value=value) for name, value in raw_headers.multi_items()]
        )


class ItemResource(Resource, path="/items"):
    async def create(self, json: Item) -> Item:
        return json

    async def read_many(self, params: SearchParams) -> SearchResult:
        return SearchResult(
            q=params.q,
            page=params.page,
            items=[Item(id=params.page, name=f"{params.q}-match")],
        )

    async def read_one(self, path: ItemPath) -> Item:
        return Item(id=path.item_id, name=f"item-{path.item_id}")

    async def update_full(self, path: ItemPath, json: Item) -> Item:
        return Item(id=path.item_id, name=json.name)

    async def update_partial(self, path: ItemPath, json: RenameItem) -> Item:
        return Item(id=path.item_id, name=json.name)


class EventsEndpoint(Endpoint, path="/events"):
    async def _events(self) -> AsyncIterator[ServerSentEvent[TickEvent]]:
        for index in range(25):
            yield ServerSentEvent(
                data=TickEvent(msg=f"hello {index}", now=index * 100),
                event="tick",
                id=str(index),
            )
            await asyncio.sleep(0)

    async def get(self) -> SSEResponse[TickEvent]:
        return SSEResponse(stream=self._events())


class BoomResponse(Struct):
    status: str


class BoomEndpoint(Endpoint, path="/boom"):
    async def get(self) -> BoomResponse:
        raise ValueError("simulated failure")


class App(BaseApp):
    async def wire(self) -> None:
        self._include_endpoint(SlowEndpoint())
        self._include_endpoint(EchoHeadersEndpoint())
        self._include_resource(ItemResource())
        self._include_endpoint(EventsEndpoint())
        self._include_endpoint(BoomEndpoint())


app = App()
