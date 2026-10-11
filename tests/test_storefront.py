import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import shop
from app.database import get_session
from app.main import app
from app.models import Base, Inventory, InvoiceCounter, Order, ShopOrder, Tenant
from app.worker import reclaim_expired_holds

pytestmark = pytest.mark.asyncio
ORIGIN = {"Origin": "http://test"}
PASSWORD = "local-test-password-strong"
SECRET = "razorpay-key-secret"


@pytest_asyncio.fixture
async def store(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(shop, "SessionLocal", sessions)
    monkeypatch.setattr(shop.cache_manager, "invalidate_tenant_namespace", AsyncMock())
    for key, value in {
        "STOREFRONT_MODE": "true", "STORE_SLUG": "test-store", "ADMIN_PASSWORD": PASSWORD,
        "ADMIN_SESSION_SECRET": "s" * 40, "COOKIE_SECURE": "false", "PUBLIC_URL": "",
        "RAZORPAY_KEY_ID": "rzp_test_key", "RAZORPAY_KEY_SECRET": SECRET,
        "RAZORPAY_WEBHOOK_SECRET": "webhook-secret", "SHIPPING_RATES": '{"IN":"50.00","US":"1000.00"}',
        "SELLER_ADDRESS": "123 Test Market, Bengaluru, India", "SUPPORT_EMAIL": "support@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    shop.login_attempts.clear()
    await shop.bootstrap_store()
    payments = {}

    async def gateway(method, path, data=None):
        if method == "POST":
            return {"id": "order_" + uuid.uuid4().hex}
        return payments[path.removeprefix("payments/")]

    monkeypatch.setattr(shop, "razorpay_request", gateway)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions, payments
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def sign_in(client):
    response = await client.post("/api/admin/login", headers=ORIGIN, json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]


async def product(client, sku="TEST", stock=5, active=True):
    response = await client.post("/api/admin/products", headers=ORIGIN, json={
        "sku": sku, "name": "Test product", "description": "A useful product", "category": "Home",
        "price": "100.00", "initial_stock": stock, "active": active,
    })
    assert response.status_code == 201, response.text
    return response.json()["product_id"]


def checkout_payload(product_id, quantity=2, country="IN"):
    return {"items": [{"product_id": product_id, "quantity": quantity}],
            "customer": {"name": "A Customer", "email": "buyer@example.com", "phone": "+919876543210",
                         "address": "123 Main Street", "city": "Bengaluru", "region": "Karnataka",
                         "postal_code": "560001", "country": country},
            "request_id": str(uuid.uuid4()), "access_token": uuid.uuid4().hex + uuid.uuid4().hex}


async def test_storefront_admin_auth_and_origin_protection(store):
    client, _, _ = store
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/admin")).status_code == 200
    assert (await client.get("/api/admin/products")).status_code == 401
    assert (await client.post("/api/admin/login", json={"password": PASSWORD})).status_code == 403
    await sign_in(client)
    assert (await client.get("/api/admin/products")).status_code == 200
    response = await client.post("/api/admin/products", json={"sku": "X", "name": "X", "price": "1.00"})
    assert response.status_code == 403
    assert (await client.get("/orders", headers={"X-Platform-Scope": "true"})).status_code == 404
    await client.post("/api/admin/logout", headers=ORIGIN)
    assert (await client.get("/api/admin/orders")).status_code == 401


async def test_product_visibility_and_stock_edit(store):
    client, _, _ = store
    await sign_in(client)
    product_id = await product(client)
    await product(client, "HIDDEN", active=False)
    public = await client.get("/api/products", headers={"X-Tenant-ID": str(uuid.uuid4())})
    assert [p["sku"] for p in public.json()] == ["TEST"]
    assert len((await client.get("/api/admin/products")).json()) == 2
    payload = checkout_payload(product_id)
    response = await client.post("/api/checkout", json=payload)
    assert response.status_code == 201, response.text
    catalog = (await client.get("/api/products")).json()[0]
    assert catalog["available"] == 3 and catalog["reserved"] == 2
    edited = await client.put(f"/api/admin/products/{product_id}", headers=ORIGIN, json={
        "sku": "TEST", "name": "Updated", "price": "150.00", "initial_stock": 7,
    })
    assert edited.status_code == 200, edited.text
    catalog = (await client.get("/api/products")).json()[0]
    assert catalog["available"] == 7 and catalog["reserved"] == 2


async def test_checkout_totals_retries_and_order_privacy(store):
    client, _, _ = store
    await sign_in(client)
    payload = checkout_payload(await product(client), quantity=5)
    response = await client.post("/api/checkout", json=payload)
    assert response.status_code == 201, response.text
    order = response.json()
    assert order["amount"] == 55000 and order["currency"] == "INR"
    retry = await client.post("/api/checkout", json=payload)
    assert retry.status_code == 201 and retry.json()["order_id"] == order["order_id"]
    assert (await client.get(f"/api/orders/{order['order_id']}")).status_code == 404
    detail = await client.get(f"/api/orders/{order['order_id']}", headers={"X-Order-Token": payload["access_token"]})
    assert detail.json()["customer"]["name"] == "A Customer"
    receipt = await client.get(f"/api/orders/{order['order_id']}/receipt", headers={"X-Order-Token": payload["access_token"]})
    assert receipt.status_code == 409
    payload["access_token"] = "x" * 64
    assert (await client.post("/api/checkout", json=payload)).status_code == 409


async def test_gateway_failure_rolls_back_reservation(store, monkeypatch):
    client, sessions, _ = store
    await sign_in(client)
    product_id = await product(client)

    async def unavailable(*args, **kwargs):
        raise HTTPException(503, "Payments unavailable")

    monkeypatch.setattr(shop, "razorpay_request", unavailable)
    response = await client.post("/api/checkout", json=checkout_payload(product_id))
    assert response.status_code == 503
    async with sessions() as session:
        inventory = await session.scalar(select(Inventory))
        assert inventory.available == 5 and inventory.reserved == 0
        assert await session.scalar(select(Order)) is None


async def test_unsupported_country_and_overselling_are_rejected(store):
    client, _, _ = store
    await sign_in(client)
    product_id = await product(client)
    assert (await client.post("/api/checkout", json=checkout_payload(product_id, country="GB"))).status_code == 422
    assert (await client.post("/api/checkout", json=checkout_payload(product_id, quantity=6))).status_code == 409
    response = await client.post("/api/checkout", json=checkout_payload(product_id, country="US"))
    assert response.json()["amount"] == 120000


async def test_verified_payment_receipt_and_fulfilment(store):
    client, _, payments = store
    await sign_in(client)
    payload = checkout_payload(await product(client))
    order = (await client.post("/api/checkout", json=payload)).json()
    token = {"X-Order-Token": payload["access_token"]}
    payment_id = "pay_test"
    payments[payment_id] = {"id": payment_id, "order_id": order["gateway_order_id"], "amount": order["amount"],
                            "currency": "INR", "status": "captured"}
    signature = hmac.new(SECRET.encode(), f"{order['gateway_order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    verification = {"razorpay_order_id": order["gateway_order_id"], "razorpay_payment_id": payment_id, "razorpay_signature": signature}
    path = f"/api/orders/{order['order_id']}/verify"
    forged = await client.post(path, headers=token, json={**verification, "razorpay_signature": "0" * 64})
    assert forged.status_code == 401
    verified = await client.post(path, headers=token, json=verification)
    assert verified.status_code == 200, verified.text
    assert (await client.post(path, headers=token, json=verification)).status_code == 200
    order_data = (await client.get(f"/api/orders/{order['order_id']}", headers=token)).json()
    assert order_data["invoice_number"].startswith("INV-") and order_data["invoice_number"].endswith("000001")
    catalog = (await client.get("/api/products")).json()[0]
    assert catalog["available"] == 3 and catalog["reserved"] == 0
    receipt = await client.get(f"/api/orders/{order['order_id']}/receipt", headers=token)
    assert receipt.status_code == 200 and "INR 250.00" in receipt.text
    admin_invoice = await client.get(f"/api/admin/orders/{order['order_id']}/invoice")
    assert admin_invoice.status_code == 200 and order_data["invoice_number"] in admin_invoice.text
    summary = (await client.get("/api/admin/summary")).json()
    assert summary["paid_orders"] == 1 and summary["revenue"] == "250.00"
    updated = await client.patch(f"/api/admin/orders/{order['order_id']}", headers=ORIGIN,
                                 json={"status": "SHIPPED", "tracking_url": "https://example.com/tracking"})
    assert updated.status_code == 200
    assert (await client.get(f"/api/orders/{order['order_id']}", headers=token)).json()["fulfilment"] == "SHIPPED"


async def test_webhook_signature_amount_validation_and_replay(store):
    client, _, _ = store
    await sign_in(client)
    payload = checkout_payload(await product(client))
    order = (await client.post("/api/checkout", json=payload)).json()

    async def send(amount, signature_valid=True):
        body = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
            "id": "pay_webhook", "order_id": order["gateway_order_id"], "amount": amount,
            "currency": "INR", "status": "captured"}}}}).encode()
        signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest() if signature_valid else "forged"
        return await client.post("/api/payments/webhook", content=body, headers={"X-Razorpay-Signature": signature})

    assert (await send(order["amount"], False)).status_code == 401
    assert (await send(1)).status_code == 409
    assert (await send(order["amount"])).status_code == 200
    assert (await send(order["amount"])).status_code == 200
    assert (await client.get("/api/products")).json()[0]["reserved"] == 0


async def test_login_throttle_and_unconfigured_credentials(store, monkeypatch):
    client, _, _ = store
    for _ in range(5):
        assert (await client.post("/api/admin/login", headers=ORIGIN, json={"password": "wrong"})).status_code == 401
    assert (await client.post("/api/admin/login", headers=ORIGIN, json={"password": PASSWORD})).status_code == 429
    monkeypatch.delenv("ADMIN_SESSION_SECRET")
    assert (await client.get("/api/admin/orders")).status_code == 503


async def test_checkout_uses_server_prices_and_combines_duplicate_lines(store):
    client, _, _ = store
    await sign_in(client)
    product_id = await product(client)
    payload = checkout_payload(product_id, quantity=1)
    payload["items"].append({"product_id": product_id, "quantity": 1, "price": "0.01"})
    payload["total"] = "0.02"
    response = await client.post("/api/checkout", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["amount"] == 25000
    inventory = (await client.get("/api/products")).json()[0]
    assert inventory["available"] == 3 and inventory["reserved"] == 2


async def test_late_capture_after_reclamation_does_not_consume_restored_stock(store):
    client, sessions, payments = store
    await sign_in(client)
    payload = checkout_payload(await product(client))
    order = (await client.post("/api/checkout", json=payload)).json()
    async with sessions() as session:
        await session.execute(update(Order).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        await session.commit()
        assert await reclaim_expired_holds(session) == 1
    payment_id = "pay_late"
    payments[payment_id] = {"id": payment_id, "order_id": order["gateway_order_id"], "amount": order["amount"],
                            "currency": "INR", "status": "captured"}
    signature = hmac.new(SECRET.encode(), f"{order['gateway_order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    verification = {"razorpay_order_id": order["gateway_order_id"], "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature}
    response = await client.post(f"/api/orders/{order['order_id']}/verify",
                                 headers={"X-Order-Token": payload["access_token"]}, json=verification)
    assert response.status_code == 409
    inventory = (await client.get("/api/products")).json()[0]
    assert inventory["available"] == 5 and inventory["reserved"] == 0


async def test_receipt_escapes_customer_html(store):
    client, _, payments = store
    await sign_in(client)
    payload = checkout_payload(await product(client))
    payload["customer"]["name"] = '<script>alert("x")</script>'
    order = (await client.post("/api/checkout", json=payload)).json()
    payments["pay_escape"] = {"id": "pay_escape", "order_id": order["gateway_order_id"],
                              "amount": order["amount"], "currency": "INR", "status": "captured"}
    signature = hmac.new(SECRET.encode(), f"{order['gateway_order_id']}|pay_escape".encode(), hashlib.sha256).hexdigest()
    token = {"X-Order-Token": payload["access_token"]}
    response = await client.post(f"/api/orders/{order['order_id']}/verify", headers=token, json={
        "razorpay_order_id": order["gateway_order_id"], "razorpay_payment_id": "pay_escape", "razorpay_signature": signature})
    assert response.status_code == 200
    receipt = await client.get(f"/api/orders/{order['order_id']}/receipt", headers=token)
    assert '<script>' not in receipt.text and '&lt;script&gt;' in receipt.text


async def test_invoice_numbers_are_sequential_and_replays_reuse_number(store):
    client, sessions, payments = store
    await sign_in(client)
    product_id = await product(client)
    numbers = []
    for index in range(2):
        payload = checkout_payload(product_id, quantity=1)
        order = (await client.post("/api/checkout", json=payload)).json()
        payment_id = f"pay_invoice{index}"
        payments[payment_id] = {"id": payment_id, "order_id": order["gateway_order_id"],
                                "amount": order["amount"], "currency": "INR", "status": "captured"}
        signature = hmac.new(SECRET.encode(), f"{order['gateway_order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        verification = {"razorpay_order_id": order["gateway_order_id"], "razorpay_payment_id": payment_id,
                        "razorpay_signature": signature}
        token = {"X-Order-Token": payload["access_token"]}
        for _ in range(2):
            response = await client.post(f"/api/orders/{order['order_id']}/verify", headers=token, json=verification)
            assert response.status_code == 200
        data = (await client.get(f"/api/orders/{order['order_id']}", headers=token)).json()
        numbers.append(data["invoice_number"])
        invoice = await client.get(f"/api/orders/{order['order_id']}/invoice", headers=token)
        assert invoice.status_code == 200 and data["invoice_number"] in invoice.text
        assert "GST charged: INR 0.00" in invoice.text
    assert numbers[0].endswith("000001") and numbers[1].endswith("000002")
    async with sessions() as session:
        assert (await session.scalar(select(InvoiceCounter))).last_number == 2


async def test_checkout_is_disabled_until_seller_details_are_configured(store, monkeypatch):
    client, sessions, _ = store
    await sign_in(client)
    product_id = await product(client)
    monkeypatch.delenv("SELLER_ADDRESS")
    assert (await client.get("/api/store")).json()["payments_ready"] is False
    assert (await client.post("/api/checkout", json=checkout_payload(product_id))).status_code == 503
    async with sessions() as session:
        assert await session.scalar(select(Order)) is None
        assert (await session.scalar(select(Inventory))).available == 5
