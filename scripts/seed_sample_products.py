"""Seed sample products for local development."""
import asyncio
import os
from sqlalchemy import select
from app.database import SessionLocal, engine
from app.models import Tenant, Product, Inventory
from app.shop import bootstrap_store


async def main():
    await bootstrap_store()
    target_slug = os.getenv("STORE_SLUG", "xyz-store")
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == target_slug))
        if not tenant:
            print(f"No tenant found with slug {target_slug}.")
            return
        existing = await session.scalar(select(Product).where(Product.tenant_id == tenant.tenant_id))
        if existing:
            print("Store already contains products.")
            return

        items = [
            ("MUG", "Everyday Ceramic Mug", "Home", "499.00", "A little warmth for your morning ritual.", 20),
            ("TEE", "Classic Cotton Tee", "Clothing", "899.00", "Soft cotton. Easy comfort. Your everyday essential.", 15),
            ("LIGHT", "Minimal Desk Light", "Electronics", "1299.00", "Bring a warm glow to your favourite corner.", 10),
            ("BAG", "Canvas Backpack", "Travel", "1599.00", "Take the everyday essentials wherever you go.", 8),
        ]
        for sku, name, cat, price, desc, stock in items:
            p = Product(tenant_id=tenant.tenant_id, sku=sku, name=name, category=cat, price=price, description=desc, active=True)
            session.add(p)
            await session.flush()
            session.add(Inventory(tenant_id=tenant.tenant_id, product_id=p.product_id, available=stock, reserved=0))
        await session.commit()
        print(f"Successfully seeded {len(items)} sample products for {tenant.name} ({tenant.slug}).")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

