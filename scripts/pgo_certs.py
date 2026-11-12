#!yeet
"""Writes `scripts/pgo_train.py`'s HTTPS certificates into `directory`, from trustme as the tests
make theirs: `ca.pem`, and `server.pem` (a key and chain for `localhost`/`127.0.0.1`).

A separate step because trustme needs cryptography, which has no wheel for every interpreter a
wheel is trained on (3.15t), so this runs on any GIL-enabled one instead.
"""

from pathlib import Path

import trustme


def main(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    ca = trustme.CA()
    ca.cert_pem.write_to_path(directory / "ca.pem")
    issued = ca.issue_cert("localhost", "127.0.0.1")
    issued.private_key_and_cert_chain_pem.write_to_path(directory / "server.pem")
