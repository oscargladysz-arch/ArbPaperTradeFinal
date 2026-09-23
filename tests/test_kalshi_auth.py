from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pmcore.venues.kalshi.auth import KalshiSigner


def test_signature_verifies_and_strips_query(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "k.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    s = KalshiSigner("kid", pem)
    h = s.sign("get", "/trade-api/v2/historical/trades?limit=5", timestamp_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    msg = b"1700000000000GET/trade-api/v2/historical/trades"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
