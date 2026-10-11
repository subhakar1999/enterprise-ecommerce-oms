"""Update sample products with high quality HTTPS product images."""
import asyncio
import os
from sqlalchemy import select
from app.database import SessionLocal, engine
from app.models import Tenant, Product, ShopProduct
from app.redis_client import cache_manager


IMAGE_MAP = {
    "MUG": "https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?auto=format&fit=crop&w=600&q=80",
    "TEE": "https://images.unsplash.com/photo-1521572267360-ee0c2909d518?auto=format&fit=crop&w=600&q=80",
    "LIGHT": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=600&q=80",
    "BAG": "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?auto=format&fit=crop&w=600&q=80",
}


async def main():
    target_slug = os.getenv("STORE_SLUG", "xyz-store")
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == target_slug))
        if not tenant:
            print(f"Store not found: {target_slug}")
            return

        products = (await session.scalars(select(Product).where(Product.tenant_id == tenant.tenant_id))).all()
        updated_count = 0

        for p in products:
            image_url = IMAGE_MAP.get(p.sku.upper())
            if not image_url:
                continue

            shop_product = await session.get(ShopProduct, p.product_id)
            if shop_product is None:
                shop_product = ShopProduct(product_id=p.product_id, image_url=image_url)
                session.add(shop_product)
            else:
                shop_product.image_url = image_url

            updated_count += 1
            print(f"Set image for {p.name} ({p.sku})")

        await session.commit()
        print(f"Updated {updated_count} product images.")

        try:
            await cache_manager.invalidate_tenant_namespace(str(tenant.tenant_id), "products")
            print("Invalidated products cache successfully.")
        except Exception as e:
            print(f"Cache invalidation note: {e}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

