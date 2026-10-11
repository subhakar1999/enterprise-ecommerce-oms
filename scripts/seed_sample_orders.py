"""Seed sample orders for testing the admin and storefront order experience."""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from app.database import SessionLocal, engine
from app.models import Tenant, Product, Order, ShopOrder, OrderItem, OrderStatus, InvoiceCounter
from app.shop import token_hash


async def main():
    target_slug = os.getenv("STORE_SLUG", "xyz-store")
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == target_slug))
        if not tenant:
            print(f"Store not found: {target_slug}")
            return

        products = {p.sku.upper(): p for p in (await session.scalars(select(Product).where(Product.tenant_id == tenant.tenant_id))).all()}
        if not products:
            print("No products available to create orders.")
            return

        existing_orders = await session.scalar(select(Order).where(Order.tenant_id == tenant.tenant_id))
        if existing_orders:
            print("Orders already exist in the store.")
            return

        now = datetime.now(timezone.utc)
        seller = {"name": tenant.name, "address": "123 Demo Market, Bengaluru, India", "email": "support@example.com"}

        # Order 1: Paid, UNFULFILLED
        o1_id = uuid.uuid4()
        items1 = []
        total1 = Decimal("0.00")
        for sku, qty in [("BAG", 1), ("TEE", 2)]:
            if sku in products:
                p = products[sku]
                items1.append((p, qty))
                total1 += p.price * qty

        order1 = Order(order_id=o1_id, tenant_id=tenant.tenant_id, status=OrderStatus.PAYMENT_CAPTURED,
                       total_amount=total1, created_at=now, expires_at=None)
        session.add(order1)
        await session.flush()

        detail1 = ShopOrder(
            order_id=o1_id,
            request_id=uuid.uuid4(),
            token_hash=token_hash("test-token-1"),
            customer_json=json.dumps({
                "name": "Priya Sharma", "email": "priya@example.com", "phone": "+91 98765 43210",
                "address": "42 Indiranagar 100ft Road", "city": "Bengaluru", "region": "Karnataka",
                "postal_code": "560038", "country": "IN"
            }),
            seller_json=json.dumps(seller),
            gateway_order_id="order_seed_001",
            gateway_payment_id="pay_seed_001",
            shipping_amount=Decimal("0.00"),
            fulfilment="UNFULFILLED",
            tracking_url="",
            invoice_number="XYZ-2627-0001",
            paid_at=now
        )
        session.add(detail1)
        for p, qty in items1:
            session.add(OrderItem(tenant_id=tenant.tenant_id, order_id=o1_id, product_id=p.product_id,
                                  sku=p.sku, quantity=qty, unit_price=p.price))

        # Order 2: Paid, SHIPPED
        o2_id = uuid.uuid4()
        items2 = []
        total2 = Decimal("0.00")
        for sku, qty in [("MUG", 2), ("LIGHT", 1)]:
            if sku in products:
                p = products[sku]
                items2.append((p, qty))
                total2 += p.price * qty

        order2 = Order(order_id=o2_id, tenant_id=tenant.tenant_id, status=OrderStatus.PAYMENT_CAPTURED,
                       total_amount=total2, created_at=now, expires_at=None)
        session.add(order2)
        await session.flush()

        detail2 = ShopOrder(
            order_id=o2_id,
            request_id=uuid.uuid4(),
            token_hash=token_hash("test-token-2"),
            customer_json=json.dumps({
                "name": "Arun Kumar", "email": "arun@example.com", "phone": "+91 98765 12345",
                "address": "15 MG Road", "city": "Bengaluru", "region": "Karnataka",
                "postal_code": "560001", "country": "IN"
            }),
            seller_json=json.dumps(seller),
            gateway_order_id="order_seed_002",
            gateway_payment_id="pay_seed_002",
            shipping_amount=Decimal("0.00"),
            fulfilment="SHIPPED",
            tracking_url="https://track.example.com/xyz1234",
            invoice_number="XYZ-2627-0002",
            paid_at=now
        )
        session.add(detail2)
        for p, qty in items2:
            session.add(OrderItem(tenant_id=tenant.tenant_id, order_id=o2_id, product_id=p.product_id,
                                  sku=p.sku, quantity=qty, unit_price=p.price))

        # Order 3: PENDING_PAYMENT
        o3_id = uuid.uuid4()
        items3 = []
        total3 = Decimal("0.00")
        if "LIGHT" in products:
            p = products["LIGHT"]
            items3.append((p, 1))
            total3 += p.price

        order3 = Order(order_id=o3_id, tenant_id=tenant.tenant_id, status=OrderStatus.PENDING_PAYMENT,
                       total_amount=total3, created_at=now, expires_at=now)
        session.add(order3)
        await session.flush()

        detail3 = ShopOrder(
            order_id=o3_id,
            request_id=uuid.uuid4(),
            token_hash=token_hash("test-token-3"),
            customer_json=json.dumps({
                "name": "Vikram Mehta", "email": "vikram@example.com", "phone": "+91 98765 99999",
                "address": "78 Bandra West", "city": "Mumbai", "region": "Maharashtra",
                "postal_code": "400050", "country": "IN"
            }),
            seller_json=json.dumps(seller),
            gateway_order_id="order_seed_003",
            gateway_payment_id=None,
            shipping_amount=Decimal("0.00"),
            fulfilment="UNFULFILLED",
            tracking_url="",
            invoice_number=None,
            paid_at=None
        )
        session.add(detail3)
        for p, qty in items3:
            session.add(OrderItem(tenant_id=tenant.tenant_id, order_id=o3_id, product_id=p.product_id,
                                  sku=p.sku, quantity=qty, unit_price=p.price))

        # Update counter
        counter = await session.get(InvoiceCounter, (tenant.tenant_id, "2026-27"))
        if counter:
            counter.last_number = max(counter.last_number, 2)
        else:
            session.add(InvoiceCounter(tenant_id=tenant.tenant_id, financial_year="2026-27", last_number=2))

        await session.commit()
        print("Successfully seeded 3 sample orders.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

