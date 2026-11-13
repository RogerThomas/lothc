from ._client import HTTPClient
from ._errors import (
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
)
from ._headers import CaseInsensitiveDict
from ._oauth import (
    OAuthProvider,
    OAuthTokenError,
    SyncOAuthProvider,
    TokenRefreshRequestTyping,
    TokenRequestTyping,
    TokenResponseTyping,
)
from ._response import (
    RequestInfo,
    Response,
)
from ._settings import NativeMode, TlsVersion
from ._sse import SSEEvent
from ._sync_client import SyncHTTPClient
from ._types import (
    AuthProvider,
    Data,
    File,
    Form,
    Headers,
    JSONPayload,
    Params,
    SyncAuthProvider,
    TypedHeaders,
)

__all__ = [
    "AuthProvider",
    "CaseInsensitiveDict",
    "Data",
    "File",
    "Form",
    "HTTPClient",
    "HTTPConnectionError",
    "HTTPResponseError",
    "HTTPTimeoutError",
    "HTTPTransportError",
    "Headers",
    "JSONPayload",
    "NativeMode",
    "OAuthProvider",
    "OAuthTokenError",
    "Params",
    "RequestInfo",
    "Response",
    "SSEEvent",
    "SyncAuthProvider",
    "SyncHTTPClient",
    "SyncOAuthProvider",
    "TlsVersion",
    "TokenRefreshRequestTyping",
    "TokenRequestTyping",
    "TokenResponseTyping",
    "TypedHeaders",
]


# Report every public class as `lothc.<Name>`, the path callers import it from, rather than the
# private module it lives in, so tracebacks and reprs read `lothc.HTTPResponseError` (httpx does
# the same). Pickle still finds each class, since `lothc` exports it.
def _report_as_public(namespace: dict[str, object], names: list[str], module: str) -> None:
    for name in names:
        exported = namespace[name]
        if isinstance(exported, type):
            exported.__module__ = module


_report_as_public(globals(), __all__, __name__)
