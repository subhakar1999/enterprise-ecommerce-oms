"""Loopback-only browser test server. SQLite and simulated payments; never deploy.

Run with .venv/Scripts/python.exe scripts/browser_fixture.py from the project root.
"""
import asyncio
import hashlib
import hmac
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
preview = ROOT / ".local-preview"
preview.mkdir(exist_ok=True)
database = preview / ("browser-" + uuid.uuid4().hex + ".db")
os.environ.update({
    "DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix(),
    "STOREFRONT_MODE": "true", "WORKER_ENABLED": "false", "AUTO_CREATE_SCHEMA": "true",
    "STORE_SLUG": "browser-test", "STORE_NAME": "XYZ Store", "COOKIE_SECURE": "false",
    "ADMIN_PASSWORD": "Browser-test-password-2026", "ADMIN_SESSION_SECRET": "browser-only-secret-" + uuid.uuid4().hex,
    "RAZORPAY_KEY_ID": "rzp_test_browser", "RAZORPAY_KEY_SECRET": "browser-gateway-secret",
    "SHIPPING_RATES": '{"IN":"50.00","US":"1000.00"}', "PUBLIC_URL": "http://127.0.0.1:8012",
    "SELLER_ADDRESS": "123 Demo Market, Bengaluru, India", "SUPPORT_EMAIL": "demo@example.com",
})

import uvicorn
from fastapi import HTTPException
from sqlalchemy import select
from app import shop
from app.database import SessionLocal, engine
from app.main import app
from app.models import Base, Inventory, Product, Tenant

gateway_orders = {}
payments = {}


async def gateway(method, path, data=None):
    if method == "POST":
        order_id = "order_" + uuid.uuid4().hex
        gateway_orders[order_id] = data
        return {"id": order_id}
    return payments[path.removeprefix("payments/")]


async def no_cache(*args, **kwargs):
    pass


shop.razorpay_request = gateway
shop.cache_manager.invalidate_tenant_namespace = no_cache


@app.post("/api/__test/payment")
async def simulated_payment(payload: dict):
    order_id = payload.get("order_id")
    if order_id not in gateway_orders:
        raise HTTPException(404)
    payment_id = "pay_" + uuid.uuid4().hex
    order = gateway_orders[order_id]
    payments[payment_id] = {"id": payment_id, "order_id": order_id, "amount": order["amount"],
                            "currency": order["currency"], "status": "captured"}
    signature = hmac.new(b"browser-gateway-secret", f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return {"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": signature}


async def seed():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await shop.bootstrap_store()
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant))
        for sku, name, category, price, description in [
            ("MUG", "Everyday ceramic mug", "Home", "499.00", "A little warmth for your morning ritual."),
            ("TEE", "Classic cotton tee", "Clothing", "899.00", "Soft cotton. Easy comfort. Your everyday essential."),
            ("LIGHT", "Minimal desk light", "Electronics", "1299.00", "Bring a warm glow to your favourite corner."),
        ]:
            product = Product(tenant_id=tenant.tenant_id, sku=sku, name=name, category=category,
                              price=price, description=description)
            session.add(product)
            await session.flush()
            session.add(Inventory(tenant_id=tenant.tenant_id, product_id=product.product_id, available=12, reserved=0))
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
    uvicorn.run(app, host="127.0.0.1", port=8012)
