import hashlib
import hmac
import os


def generate_signature(payload: bytes, secret: str | None = None) -> str:
    key = (secret or os.getenv("PAYMENT_WEBHOOK_SECRET", "")).encode("utf-8")
    if not key:
        raise RuntimeError("PAYMENT_WEBHOOK_SECRET must be configured")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str, secret: str | None = None) -> bool:
    expected = generate_signature(payload, secret)
    supplied = signature.removeprefix("sha256=")
    try:
        supplied_bytes = supplied.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), supplied_bytes)
