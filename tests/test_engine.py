import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, Inventory, Order, Tenant
from app.payment import generate_signature, verify_signature
from app.redis_client import redis_client
from app.worker import reclaim_expired_holds

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def database_schema():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await redis_client.aclose()
    await engine.dispose()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def tenant(client: httpx.AsyncClient, slug: str) -> tuple[uuid.UUID, dict[str, str]]:
    response = await client.post(
        "/platform/tenants",
        headers={"X-Platform-Scope": "true"},
        json={"slug": f"{slug}-{uuid.uuid4().hex[:8]}", "name": slug},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return uuid.UUID(data["tenant_id"]), {"X-Tenant-ID": data["tenant_id"]}


async def create_product(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    sku: str,
    name: str = "Test product",
    price: str = "10.00",
    stock: int = 2,
    category: str = "general",
) -> dict:
    response = await client.post(
        "/products",
        headers=headers,
        json={
            "sku": sku,
            "name": name,
            "description": "Integration fixture",
            "category": category,
            "price": price,
            "initial_stock": stock,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def checkout(
    client: httpx.AsyncClient, headers: dict[str, str], product_id: str, quantity: int = 1
) -> httpx.Response:
    return await client.post(
        "/checkout",
        headers=headers,
        json={"items": [{"product_id": product_id, "quantity": quantity}]},
    )


@pytest_asyncio.fixture(autouse=True)
async def cleanup_tenants(database_schema):
    created_tenants: list[uuid.UUID] = []
    yield created_tenants
    if created_tenants:
        async with SessionLocal() as session:
            await session.execute(delete(Tenant).where(Tenant.tenant_id.in_(created_tenants)))
            await session.commit()


async def test_full_system_isolation_and_concurrency(client, cleanup_tenants):
    alpha_id, alpha_headers = await tenant(client, "alpha-corp")
    beta_id, beta_headers = await tenant(client, "beta-tech")
    cleanup_tenants.extend([alpha_id, beta_id])
    alpha_product = await create_product(client, alpha_headers, sku="SHARED-SKU", stock=2)
    beta_product = await create_product(client, beta_headers, sku="SHARED-SKU", stock=5)
    assert alpha_product["sku"] == beta_product["sku"]

    responses = await asyncio.gather(
        *(checkout(client, alpha_headers, alpha_product["product_id"]) for _ in range(10))
    )
    assert sorted(response.status_code for response in responses) == [201, 201] + [409] * 8
    async with SessionLocal() as session:
        inventory = await session.scalar(
            select(Inventory).where(
                Inventory.tenant_id == alpha_id,
                Inventory.product_id == uuid.UUID(alpha_product["product_id"]),
            )
        )
        assert inventory is not None
        assert inventory.available == 0
        beta_inventory = await session.scalar(
            select(Inventory).where(
                Inventory.tenant_id == beta_id,
                Inventory.product_id == uuid.UUID(beta_product["product_id"]),
            )
        )
        assert beta_inventory.available == 5


async def test_phase_two_features_caching_categories_and_hold_reclamation(
    client, cleanup_tenants
):
    tenant_id, headers = await tenant(client, "phase-two")
    cleanup_tenants.append(tenant_id)
    product = await create_product(
        client, headers, sku="RECLAIM", stock=3, category="seasonal"
    )
    miss = await client.get("/products?category=seasonal", headers=headers)
    hit = await client.get("/products?category=seasonal", headers=headers)
    assert miss.status_code == 200 and miss.headers["X-Cache"] == "MISS"
    assert hit.status_code == 200 and hit.headers["X-Cache"] == "HIT"
    assert len(miss.json()) == 1
    order_response = await checkout(client, headers, product["product_id"], 2)
    assert order_response.status_code == 201
    order_id = uuid.UUID(order_response.json()["order_id"])
    async with SessionLocal() as session:
        await session.execute(
            update(Order)
            .where(Order.tenant_id == tenant_id, Order.order_id == order_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()
    assert await reclaim_expired_holds() == 1
    async with SessionLocal() as session:
        inventory = await session.scalar(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id,
                Inventory.product_id == uuid.UUID(product["product_id"]),
            )
        )
        assert inventory.available == 3
        assert inventory.reserved == 0


async def test_phase_three_payment_webhooks_and_idempotency(
    client, cleanup_tenants, monkeypatch
):
    secret = "test-webhook-secret"
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", secret)
    tenant_id, headers = await tenant(client, "phase-three")
    cleanup_tenants.append(tenant_id)
    product = await create_product(client, headers, sku="PAYMENT", stock=4)
    order_response = await checkout(client, headers, product["product_id"], 2)
    order_id = order_response.json()["order_id"]
    event = {
        "id": f"evt-{uuid.uuid4()}",
        "type": "payment_intent.payment_failed",
        "data": {"order_id": order_id},
    }
    body = json.dumps(event, separators=(",", ":")).encode()
    signature = generate_signature(body, secret)
    assert verify_signature(body, signature, secret)
    forged = await client.post(
        "/webhooks/payments",
        headers={**headers, "X-Payment-Signature": "0" * 64},
        content=body,
    )
    assert forged.status_code == 401
    processed = await client.post(
        "/webhooks/payments",
        headers={**headers, "X-Payment-Signature": signature},
        content=body,
    )
    assert processed.status_code == 200
    assert processed.json()["status"] == "processed"
    duplicate = await client.post(
        "/webhooks/payments",
        headers={**headers, "X-Payment-Signature": signature},
        content=body,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate_ignored"
    async with SessionLocal() as session:
        inventory = await session.scalar(
            select(Inventory).where(
                Inventory.tenant_id == tenant_id,
                Inventory.product_id == uuid.UUID(product["product_id"]),
            )
        )
        assert inventory.available == 4
        assert inventory.reserved == 0


async def test_phase_four_store_analytics_and_reporting(
    client, cleanup_tenants, monkeypatch
):
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret")
    store_a_id, store_a_headers = await tenant(client, "analytics-a")
    store_b_id, store_b_headers = await tenant(client, "analytics-b")
    cleanup_tenants.extend([store_a_id, store_b_id])
    product = await create_product(
        client, store_a_headers, sku="ANALYTICS", price="12.50", stock=5
    )
    first_order = await checkout(client, store_a_headers, product["product_id"], 2)
    second_order = await checkout(client, store_a_headers, product["product_id"], 1)
    assert first_order.status_code == second_order.status_code == 201
    event = {
        "id": f"evt-{uuid.uuid4()}",
        "type": "payment_intent.succeeded",
        "data": {"order_id": first_order.json()["order_id"]},
    }
    body = json.dumps(event, separators=(",", ":")).encode()
    signature = generate_signature(body)
    captured = await client.post(
        "/webhooks/payments",
        headers={**store_a_headers, "X-Payment-Signature": signature},
        content=body,
    )
    assert captured.status_code == 200

    denied = await client.get(
        "/analytics/store", headers={**store_a_headers, "X-Role": "CUSTOMER"}
    )
    assert denied.status_code == 403
    store_a = await client.get("/analytics/store", headers=store_a_headers)
    store_b = await client.get("/analytics/store", headers=store_b_headers)
    assert store_a.status_code == store_b.status_code == 200
    metrics_a = store_a.json()
    metrics_b = store_b.json()
    assert Decimal(metrics_a["gross_revenue"]) == Decimal("37.50")
    assert Decimal(metrics_a["captured_revenue"]) == Decimal("25.00")
    assert Decimal(metrics_a["average_order_value"]) == Decimal("25.00")
    assert metrics_a["total_orders"] == 2
    assert metrics_a["conversion_rate"] == 0.5
    assert metrics_a["top_selling_skus"][0]["sku"] == "ANALYTICS"
    assert Decimal(metrics_b["gross_revenue"]) == Decimal("0")
    assert Decimal(metrics_b["captured_revenue"]) == Decimal("0")
    assert metrics_b["total_orders"] == 0


async def test_catalog_category_all_does_not_collide(client, cleanup_tenants):
    tenant_id, headers = await tenant(client, "cache-collision")
    cleanup_tenants.append(tenant_id)
    await create_product(client, headers, sku="ALL", category="all")
    await create_product(client, headers, sku="OTHER", category="other")
    all_products = await client.get("/products", headers=headers)
    category = await client.get("/products?category=all", headers=headers)
    assert len(all_products.json()) == 2
    assert [product["sku"] for product in category.json()] == ["ALL"]
    assert category.headers["X-Cache"] == "MISS"


async def test_concurrent_payment_delivery_and_expiry(client, cleanup_tenants, monkeypatch):
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", "regression-secret")
    tenant_id, headers = await tenant(client, "payment-race")
    cleanup_tenants.append(tenant_id)
    product = await create_product(client, headers, sku="RACE", stock=3)
    response = await checkout(client, headers, product["product_id"])
    order_id = response.json()["order_id"]
    event = {"id": f"evt-{uuid.uuid4()}", "type": "payment_intent.succeeded", "data": {"order_id": order_id}}
    body = json.dumps(event).encode()
    payment_headers = {**headers, "X-Payment-Signature": generate_signature(body)}
    responses = await asyncio.gather(*(
        client.post("/webhooks/payments", headers=payment_headers, content=body)
        for _ in range(8)
    ))
    assert all(result.status_code == 200 for result in responses)
    assert sum(result.json()["status"] == "processed" for result in responses) == 1

    other_id, other_headers = await tenant(client, "payment-other")
    cleanup_tenants.append(other_id)
    conflict = await client.post("/webhooks/payments", content=body, headers={
        **other_headers, "X-Payment-Signature": generate_signature(body),
    })
    assert conflict.status_code == 409

    response = await checkout(client, headers, product["product_id"])
    expired_id = uuid.UUID(response.json()["order_id"])
    async with SessionLocal() as session:
        await session.execute(update(Order).where(Order.order_id == expired_id).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
    event["id"] = f"evt-{uuid.uuid4()}"
    event["data"]["order_id"] = str(expired_id)
    body = json.dumps(event).encode()
    expired = await client.post("/webhooks/payments", content=body, headers={
        **headers, "X-Payment-Signature": generate_signature(body),
    })
    assert expired.status_code == 409
    assert await reclaim_expired_holds() == 1


async def test_checkout_total_overflow_rolls_back(client, cleanup_tenants):
    tenant_id, headers = await tenant(client, "total-overflow")
    cleanup_tenants.append(tenant_id)
    product = await create_product(client, headers, sku="EXPENSIVE", price="9999999999.99", stock=2)
    response = await checkout(client, headers, product["product_id"], 2)
    assert response.status_code == 422
    catalog = await client.get("/products", headers=headers)
    assert catalog.json()[0]["available"] == 2


async def test_reclamation_batch_restores_shared_inventory(client, cleanup_tenants):
    tenant_id, headers = await tenant(client, "batch-reclaim")
    cleanup_tenants.append(tenant_id)
    first = await create_product(client, headers, sku="FIRST", stock=5)
    second = await create_product(client, headers, sku="SECOND", stock=5)
    order_ids = []
    for items in [
        [{"product_id": first["product_id"], "quantity": 2}],
        [{"product_id": second["product_id"], "quantity": 1}],
        [{"product_id": first["product_id"], "quantity": 1},
         {"product_id": second["product_id"], "quantity": 2}],
    ]:
        response = await client.post("/checkout", headers=headers, json={"items": items})
        assert response.status_code == 201
        order_ids.append(uuid.UUID(response.json()["order_id"]))
    async with SessionLocal() as session:
        await session.execute(update(Order).where(Order.order_id.in_(order_ids)).values(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
    assert await reclaim_expired_holds() == 3
    assert await reclaim_expired_holds() == 0
    async with SessionLocal() as session:
        inventories = (await session.scalars(select(Inventory).where(Inventory.tenant_id == tenant_id))).all()
        assert all(inventory.available == 5 and inventory.reserved == 0 for inventory in inventories)
