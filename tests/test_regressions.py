import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError

from app.main import app
from app.database import get_session
from app.payment import generate_signature
from app.redis_client import CacheManager
from app.schemas import CheckoutItem, ProductCreate


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    [], None, {"id": None},
    {"id": "event", "type": "payment_intent.succeeded", "data": {"order_id": 123}},
    {"id": "", "type": "payment_intent.succeeded", "data": {"order_id": str(uuid.uuid4())}},
    {"id": "x" * 201, "type": "payment_intent.succeeded", "data": {"order_id": str(uuid.uuid4())}},
])
async def test_malformed_signed_webhooks_return_400(event, monkeypatch):
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "regression-secret")
    monkeypatch.setattr("app.main.tenant_id_required", lambda: uuid.uuid4())

    async def unused_session():
        yield None

    app.dependency_overrides[get_session] = unused_session
    try:
        body = json.dumps(event).encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/webhooks/payments", content=body, headers={
                "X-Platform-Scope": "true", "X-Payment-Signature": generate_signature(body),
            })
        assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_cache_connection_failures_are_optional():
    client = AsyncMock()
    client.get.side_effect = ConnectionError("offline")
    client.set.side_effect = ConnectionError("offline")

    async def failed_scan(**kwargs):
        raise ConnectionError("offline")
        yield  # Make the mock behave like Redis's async iterator.

    client.scan_iter = failed_scan
    cache = CacheManager(client)
    assert await cache.get("key") is None
    await cache.set("key", "value", ex=60)
    await cache.invalidate_tenant_namespace("tenant", "products")


def test_integer_database_limits_are_validated():
    with pytest.raises(ValidationError):
        ProductCreate(sku="SKU", name="Product", price="1.00", initial_stock=2**31)
    with pytest.raises(ValidationError):
        CheckoutItem(product_id=uuid.uuid4(), quantity=2**31)
