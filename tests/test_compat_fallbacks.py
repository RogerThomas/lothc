"""`lothc` without its optional extras: each test runs a fresh interpreter in which importing
pydantic and/or msgspec raises, since `lothc._client` binds the real libraries once at import and
can't be re-imported in this process. CI's `test-no-extras` job (`task test-no-extras`) covers
the rest of the suite with neither extra installed at all.
"""

import subprocess
import sys
from collections.abc import Iterable


def _run_with_imports_blocked(
    blocked: Iterable[str], script: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run `script` in a fresh interpreter in which importing any of `blocked` raises."""
    prelude = (
        "import builtins\n"
        "_real_import = builtins.__import__\n"
        "def _blocking_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        f"    if name.split('.')[0] in {sorted(blocked)!r}:\n"
        "        raise ImportError(f'blocked for test: {name}')\n"
        "    return _real_import(name, globals, locals, fromlist, level)\n"
        "builtins.__import__ = _blocking_import\n"
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + script, *args], capture_output=True, text=True, check=False
    )


def test_import_lothc_succeeds_without_msgspec_or_pydantic() -> None:
    # `lothc._client` has plenty of unquoted `Decoder[Any]`/`TypeAdapter[Any]` annotations,
    # evaluated eagerly at import time, which `_compat.py`'s fallback stubs must survive.
    result = _run_with_imports_blocked({"msgspec", "pydantic"}, "import lothc\n")
    assert result.returncode == 0, result.stderr


def test_into_without_either_extra(base_url: str) -> None:
    # A fresh subprocess, for the same reason as the test above: `lothc._client` binds the real
    # libraries at import. It talks to this session's test server, which keeps serving on its
    # own thread while this one waits.
    script = (
        "import sys\n"
        "from typing import Any\n"
        "from lothc import SyncHTTPClient\n"
        "with SyncHTTPClient(base_url=sys.argv[1]) as client:\n"
        "    assert client.get('items/7', into=bytes).data.startswith(b'{')\n"
        "    assert client.get('items/7', into=dict).data['id'] == 7\n"
        "    assert client.get('items/7', into=dict[str, Any]).data['id'] == 7\n"
        "    try:\n"
        "        client.get('json-array', into=list[int])\n"
        "    except TypeError as error:\n"
        "        print(error)\n"
    )
    result = _run_with_imports_blocked({"msgspec", "pydantic"}, script, base_url)

    assert result.returncode == 0, result.stderr
    assert "install lothc[msgspec] or lothc[pydantic]" in result.stdout


def test_into_with_pydantic_only_metadata_without_pydantic(base_url: str) -> None:
    # msgspec alone would decode this form but silently skip the `Gt(0)` constraint, so with
    # pydantic absent it's a `TypeError`, never an unvalidated result. A fresh subprocess with
    # only pydantic blocked, for the same reason as `test_into_without_either_extra`.
    script = (
        "import sys\n"
        "from typing import Annotated\n"
        "import annotated_types\n"
        "from lothc import SyncHTTPClient\n"
        "with SyncHTTPClient(base_url=sys.argv[1]) as client:\n"
        "    assert client.get('json-array', into=list[Annotated[dict, 'doc']]).data\n"
        "    try:\n"
        "        client.get('json-array', into=list[Annotated[int, annotated_types.Gt(0)]])\n"
        "    except TypeError as error:\n"
        "        print(error)\n"
    )
    result = _run_with_imports_blocked({"pydantic"}, script, base_url)

    assert result.returncode == 0, result.stderr
    assert "install lothc[pydantic]" in result.stdout
