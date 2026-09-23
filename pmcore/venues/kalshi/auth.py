"""Optional Kalshi request signing for READ requests (rule 10 allows orders only on Kalshi, and
Phase 0 sends none; this module only raises the read rate limit from the unauthenticated
budget to the account tier's Read bucket, 200 tokens/s = 20 reads/s on Basic).

Scheme (https://docs.kalshi.com/getting_started/quick_start_authenticated_requests, and the
legacy engine.py): sign `timestamp_ms + METHOD + path_without_query` with RSA-PSS SHA-256
(salt length = digest length), base64 the signature, and send KALSHI-ACCESS-KEY,
KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem_path: str | Path) -> None:
        pem = Path(private_key_pem_path).read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be RSA")
        self._key = key
        self.key_id = key_id

    def sign(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path_only = path.split("?", 1)[0]
        msg = (ts + method.upper() + path_only).encode("utf-8")
        sig = self._key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }


def signer_from_env() -> KalshiSigner | None:
    import os

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if key_id and pem and Path(pem).exists():
        return KalshiSigner(key_id, pem)
    return None
