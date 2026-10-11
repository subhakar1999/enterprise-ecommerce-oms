import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Inventory, Order, OrderItem, OrderStatus
from app.redis_client import cache_manager

logger = logging.getLogger(__name__)


async def reclaim_expired_holds(session: AsyncSession | None = None) -> int:
    """Release expired holds atomically; returns the number of orders reclaimed."""
    owns_session = session is None
    db = session or SessionLocal()
    changed_tenants: set[str] = set()
    reclaimed = 0
    try:
        now = datetime.now(timezone.utc)
        async with db.begin():
            result = await db.execute(
                select(Order)
                .where(
                    Order.status == OrderStatus.PENDING_PAYMENT,
                    Order.expires_at <= now,
                )
                .order_by(Order.order_id)
                .with_for_update(skip_locked=True)
            )
            orders = list(result.scalars())
            items_by_order = {}
            for order in orders:
                items_result = await db.execute(
                    select(OrderItem)
                    .where(
                        OrderItem.tenant_id == order.tenant_id,
                        OrderItem.order_id == order.order_id,
                    )
                    .order_by(OrderItem.product_id)
                )
                items_by_order[order.order_id] = list(items_result.scalars())
            # Locks persist until the entire batch commits. Sort across all
            # orders so the batch follows checkout's inventory lock order.
            inventory_keys = {
                (item.tenant_id, item.product_id)
                for items in items_by_order.values()
                for item in items
            }
            inventories = {}
            for tenant_id, product_id in sorted(
                inventory_keys, key=lambda pair: (str(pair[0]), str(pair[1]))
            ):
                inventory_result = await db.execute(
                    select(Inventory)
                    .where(Inventory.tenant_id == tenant_id, Inventory.product_id == product_id)
                    .with_for_update()
                )
                inventories[(tenant_id, product_id)] = inventory_result.scalar_one()
            for order in orders:
                for item in items_by_order[order.order_id]:
                    inventory = inventories[(item.tenant_id, item.product_id)]
                    inventory.reserved -= item.quantity
                    inventory.available += item.quantity
                order.status = OrderStatus.CANCELLED
                order.expires_at = None
                changed_tenants.add(str(order.tenant_id))
                reclaimed += 1
        for tenant_id in changed_tenants:
            await cache_manager.invalidate_tenant_namespace(tenant_id, "products")
            await cache_manager.invalidate_tenant_namespace(tenant_id, "analytics")
        return reclaimed
    finally:
        if owns_session:
            await db.close()


async def run_reclamation_worker(interval_seconds: int = 30) -> None:
    while True:
        try:
            await reclaim_expired_holds()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expired-hold worker iteration failed; retrying after interval")
        await asyncio.sleep(interval_seconds)
