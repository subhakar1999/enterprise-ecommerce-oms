import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, engine, get_session
from app.models import (
    Base,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    PaymentTransaction,
    Product,
    Tenant,
)
from app.payment import verify_signature
from app.redis_client import cache_manager, close_redis
from app.schemas import (
    AnalyticsRead,
    CheckoutCreate,
    OrderRead,
    OrderSummary,
    PaymentEvent,
    ProductCreate,
    ProductRead,
    TenantCreate,
    TenantRead,
)
from app.worker import run_reclamation_worker
from app.shop import bootstrap_store, router as shop_router

logger = logging.getLogger(__name__)

current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)
current_tenant_slug: ContextVar[str | None] = ContextVar("current_tenant_slug", default=None)
is_platform_scope: ContextVar[bool] = ContextVar("is_platform_scope", default=False)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if os.getenv("AUTO_CREATE_SCHEMA", "true").lower() == "true":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    if os.getenv("STOREFRONT_MODE", "false").lower() == "true":
        await bootstrap_store()
    worker_task = None
    if os.getenv("WORKER_ENABLED", "true").lower() == "true":
        worker_task = asyncio.create_task(run_reclamation_worker())
    try:
        yield
    finally:
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        await close_redis()
        await engine.dispose()


app = FastAPI(
    title="XYZ Store",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(shop_router)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "frontend"), name="static")


def tenant_id_required() -> uuid.UUID:
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="A valid tenant scope is required")
    return tenant_id


async def tenant_resolution_middleware(request: Request, call_next):
    """Resolve and isolate tenant context for the lifetime of each HTTP request."""
    if request.url.path.startswith(("/api/", "/static/")) or request.url.path in {"/", "/admin", "/health", "/docs", "/openapi.json", "/redoc"}:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    if os.getenv("STOREFRONT_MODE", "false").lower() == "true":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    platform = request.headers.get("x-platform-scope", "").lower() == "true"
    tenant_id_token = current_tenant_id.set(None)
    tenant_slug_token = current_tenant_slug.set(None)
    platform_token = is_platform_scope.set(platform)
    try:
        if not platform:
            supplied_id = request.headers.get("x-tenant-id")
            supplied_slug = request.headers.get("x-tenant-slug")
            if supplied_id:
                try:
                    tenant_id = uuid.UUID(supplied_id)
                except ValueError:
                    return JSONResponse({"detail": "Invalid X-Tenant-ID"}, status_code=400)
                async with SessionLocal() as db:
                    tenant = await db.get(Tenant, tenant_id)
                if tenant is None:
                    return JSONResponse({"detail": "Tenant not found"}, status_code=404)
                current_tenant_id.set(tenant.tenant_id)
                current_tenant_slug.set(tenant.slug)
            else:
                slug = supplied_slug
                if not slug:
                    host = request.headers.get("host", "").split(":", 1)[0].lower()
                    platform_domain = os.getenv("PLATFORM_DOMAIN", "platform.com").lower()
                    if host.endswith("." + platform_domain):
                        slug = host[: -(len(platform_domain) + 1)].split(".")[-1]
                if slug:
                    async with SessionLocal() as db:
                        tenant = await db.scalar(select(Tenant).where(Tenant.slug == slug))
                    if tenant is None:
                        return JSONResponse({"detail": "Tenant not found"}, status_code=404)
                    current_tenant_id.set(tenant.tenant_id)
                    current_tenant_slug.set(tenant.slug)
        return await call_next(request)
    finally:
        current_tenant_id.reset(tenant_id_token)
        current_tenant_slug.reset(tenant_slug_token)
        is_platform_scope.reset(platform_token)


app.middleware("http")(tenant_resolution_middleware)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def store_console() -> FileResponse:
    frontend_path = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    return FileResponse(frontend_path, media_type="text/html")


@app.post("/platform/tenants", response_model=TenantRead, status_code=201)
async def create_tenant(
    payload: TenantCreate,
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    if not is_platform_scope.get():
        raise HTTPException(status_code=403, detail="Platform scope is required")
    tenant = Tenant(slug=payload.slug, name=payload.name)
    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Tenant slug already exists") from exc
    await session.refresh(tenant)
    return tenant


@app.get("/platform/tenants", response_model=list[TenantRead])
async def list_tenants(session: AsyncSession = Depends(get_session)) -> list[Tenant]:
    if not is_platform_scope.get():
        raise HTTPException(status_code=403, detail="Platform scope is required")
    return list(await session.scalars(select(Tenant).order_by(Tenant.created_at.desc(), Tenant.slug)))


@app.post("/products", response_model=ProductRead, status_code=201)
async def create_product(
    payload: ProductCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = tenant_id_required()
    product = Product(
        tenant_id=tenant_id,
        sku=payload.sku,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        price=payload.price,
    )
    session.add(product)
    try:
        await session.flush()
        session.add(
            Inventory(
                tenant_id=tenant_id,
                product_id=product.product_id,
                available=payload.initial_stock,
                reserved=0,
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="SKU already exists for this tenant") from exc
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "products")
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "analytics")
    return {
        "product_id": product.product_id,
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "price": product.price,
        "available": payload.initial_stock,
    }


@app.get("/products", response_model=list[ProductRead])
async def list_products(
    response: Response,
    category: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    tenant_id = tenant_id_required()
    cache_key = cache_manager.key(str(tenant_id), "products", f"catalog:{json.dumps(category)}")
    cached = await cache_manager.get(cache_key)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return json.loads(cached)
    query = (
        select(Product, Inventory.available)
        .join(
            Inventory,
            (Inventory.tenant_id == Product.tenant_id)
            & (Inventory.product_id == Product.product_id),
        )
        .where(Product.tenant_id == tenant_id, Product.active.is_(True))
        .order_by(Product.name)
    )
    if category is not None:
        query = query.where(Product.category == category)
    rows = (await session.execute(query)).all()
    result = [
        {
            "product_id": str(product.product_id),
            "sku": product.sku,
            "name": product.name,
            "description": product.description,
            "category": product.category,
            "price": str(product.price),
            "available": available,
        }
        for product, available in rows
    ]
    await cache_manager.set(cache_key, json.dumps(result), ex=120)
    response.headers["X-Cache"] = "MISS"
    return result


@app.get("/orders", response_model=list[OrderSummary])
async def list_orders(
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    tenant_id = tenant_id_required()
    orders = list(await session.scalars(
        select(Order).where(Order.tenant_id == tenant_id)
        .order_by(Order.created_at.desc(), Order.order_id).limit(limit)
    ))
    if not orders:
        return []
    items = await session.scalars(
        select(OrderItem).where(
            OrderItem.tenant_id == tenant_id,
            OrderItem.order_id.in_([order.order_id for order in orders]),
        ).order_by(OrderItem.sku)
    )
    items_by_order: dict[uuid.UUID, list[OrderItem]] = {}
    for item in items:
        items_by_order.setdefault(item.order_id, []).append(item)
    return [
        {"order_id": order.order_id, "status": order.status.value,
         "total_amount": order.total_amount, "created_at": order.created_at,
         "expires_at": order.expires_at, "items": items_by_order.get(order.order_id, [])}
        for order in orders
    ]


@app.post("/checkout", response_model=OrderRead, status_code=201)
async def checkout(
    payload: CheckoutCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = tenant_id_required()
    quantities: dict[uuid.UUID, int] = {}
    for item in payload.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    ordered_product_ids = sorted(quantities, key=str)
    async with session.begin():
        products_result = await session.execute(
            select(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.product_id.in_(ordered_product_ids),
                Product.active.is_(True),
            )
        )
        products = {product.product_id: product for product in products_result.scalars()}
        if len(products) != len(ordered_product_ids):
            raise HTTPException(status_code=404, detail="One or more products were not found")
        inventories: dict[uuid.UUID, Inventory] = {}
        for product_id in ordered_product_ids:
            inventory_result = await session.execute(
                select(Inventory)
                .where(
                    Inventory.tenant_id == tenant_id,
                    Inventory.product_id == product_id,
                )
                .with_for_update()
            )
            inventory = inventory_result.scalar_one_or_none()
            if inventory is None:
                raise HTTPException(status_code=409, detail="Inventory is not available")
            if inventory.available < quantities[product_id]:
                raise HTTPException(status_code=409, detail="Insufficient inventory")
            inventories[product_id] = inventory
        now = datetime.now(timezone.utc)
        total = sum(
            (products[product_id].price * quantity for product_id, quantity in quantities.items()),
            Decimal("0.00"),
        )
        if total > Decimal("9999999999.99"):
            raise HTTPException(status_code=422, detail="Order total exceeds the supported limit")
        order = Order(
            tenant_id=tenant_id,
            status=OrderStatus.PENDING_PAYMENT,
            total_amount=total,
            expires_at=now + timedelta(minutes=15),
        )
        session.add(order)
        await session.flush()
        for product_id in ordered_product_ids:
            quantity = quantities[product_id]
            inventory = inventories[product_id]
            inventory.available -= quantity
            inventory.reserved += quantity
            product = products[product_id]
            session.add(
                OrderItem(
                    tenant_id=tenant_id,
                    order_id=order.order_id,
                    product_id=product_id,
                    sku=product.sku,
                    quantity=quantity,
                    unit_price=product.price,
                )
            )
        order_id = order.order_id
        expires_at = order.expires_at
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "products")
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "analytics")
    return {
        "order_id": order_id,
        "status": OrderStatus.PENDING_PAYMENT.value,
        "total_amount": total,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@app.post("/webhooks/payments")
async def payment_webhook(
    request: Request,
    x_payment_signature: str = Header(default="", alias="X-Payment-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    tenant_id = tenant_id_required()
    body = await request.body()
    if not verify_signature(body, x_payment_signature):
        raise HTTPException(status_code=401, detail="Invalid payment signature")
    try:
        event = PaymentEvent.model_validate_json(body)
        event_id = event.id
        event_type = event.type
        order_id = event.data.order_id
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="Malformed payment event") from exc
    if event_type not in {"payment_intent.succeeded", "payment_intent.payment_failed"}:
        raise HTTPException(status_code=400, detail="Unsupported payment event")
    try:
        async with session.begin():
            existing = await session.scalar(
                select(PaymentTransaction).where(PaymentTransaction.event_id == event_id)
            )
            if existing:
                if existing.tenant_id != tenant_id or existing.order_id != order_id:
                    raise HTTPException(status_code=409, detail="Payment event conflict")
                return {"status": "duplicate_ignored"}
            order = await session.scalar(
                select(Order)
                .where(Order.tenant_id == tenant_id, Order.order_id == order_id)
                .with_for_update()
            )
            if order is None:
                raise HTTPException(status_code=404, detail="Order not found")
            # A concurrent delivery may have committed while this request waited
            # for the order lock. Recheck before inspecting the updated status.
            existing = await session.scalar(
                select(PaymentTransaction).where(PaymentTransaction.event_id == event_id)
            )
            if existing:
                if existing.tenant_id != tenant_id or existing.order_id != order_id:
                    raise HTTPException(status_code=409, detail="Payment event conflict")
                return {"status": "duplicate_ignored"}
            transaction = PaymentTransaction(
                tenant_id=tenant_id,
                order_id=order_id,
                event_id=event_id,
                event_type=event_type,
                payload=body.decode("utf-8"),
            )
            session.add(transaction)
            if order.status != OrderStatus.PENDING_PAYMENT:
                raise HTTPException(status_code=409, detail="Order is not awaiting payment")
            if order.expires_at is not None and order.expires_at <= datetime.now(timezone.utc):
                raise HTTPException(status_code=409, detail="Order payment hold has expired")
            items_result = await session.execute(
                select(OrderItem)
                .where(OrderItem.tenant_id == tenant_id, OrderItem.order_id == order_id)
                .order_by(OrderItem.product_id)
            )
            items = list(items_result.scalars())
            for item in items:
                inventory_result = await session.execute(
                    select(Inventory)
                    .where(
                        Inventory.tenant_id == tenant_id,
                        Inventory.product_id == item.product_id,
                    )
                    .with_for_update()
                )
                inventory = inventory_result.scalar_one()
                inventory.reserved -= item.quantity
                if event_type == "payment_intent.payment_failed":
                    inventory.available += item.quantity
            order.status = (
                OrderStatus.PAYMENT_CAPTURED
                if event_type == "payment_intent.succeeded"
                else OrderStatus.CANCELLED
            )
            order.expires_at = None
    except IntegrityError as exc:
        await session.rollback()
        existing = await session.scalar(
            select(PaymentTransaction).where(PaymentTransaction.event_id == event_id)
        )
        if existing and existing.tenant_id == tenant_id and existing.order_id == order_id:
            return {"status": "duplicate_ignored"}
        raise HTTPException(status_code=409, detail="Payment event conflict") from exc
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "products")
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "analytics")
    return {"status": "processed"}


@app.get("/analytics/store", response_model=AnalyticsRead)
async def store_analytics(
    x_role: str = Header(default="MERCHANT", alias="X-Role"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = tenant_id_required()
    if x_role.upper() == "CUSTOMER":
        raise HTTPException(status_code=403, detail="Store analytics require a merchant role")
    cache_key = cache_manager.key(str(tenant_id), "analytics", "store:v1")
    cached = await cache_manager.get(cache_key)
    if cached:
        return json.loads(cached)
    aggregate = await session.execute(
        select(
            func.coalesce(func.sum(Order.total_amount), 0),
            func.coalesce(
                func.sum(Order.total_amount).filter(
                    Order.status == OrderStatus.PAYMENT_CAPTURED
                ),
                0,
            ),
            func.count(Order.order_id),
            func.count(Order.order_id).filter(
                Order.status == OrderStatus.PAYMENT_CAPTURED
            ),
        ).where(
            Order.tenant_id == tenant_id,
            Order.status != OrderStatus.CANCELLED,
        )
    )
    gross, captured, total_orders, captured_orders = aggregate.one()
    top_result = await session.execute(
        select(
            Product.sku,
            func.sum(OrderItem.quantity).label("settled_units"),
            func.sum(OrderItem.quantity * OrderItem.unit_price).label("revenue"),
        )
        .join(
            OrderItem,
            (OrderItem.tenant_id == Product.tenant_id)
            & (OrderItem.product_id == Product.product_id),
        )
        .join(
            Order,
            (Order.tenant_id == OrderItem.tenant_id)
            & (Order.order_id == OrderItem.order_id),
        )
        .where(
            Product.tenant_id == tenant_id,
            Order.status == OrderStatus.PAYMENT_CAPTURED,
        )
        .group_by(Product.sku)
        .order_by(func.sum(OrderItem.quantity).desc(), func.sum(OrderItem.quantity * OrderItem.unit_price).desc())
        .limit(10)
    )
    top_skus = [
        {"sku": sku, "settled_units": units, "revenue": str(revenue)}
        for sku, units, revenue in top_result.all()
    ]
    aov = Decimal(captured) / captured_orders if captured_orders else Decimal("0.00")
    conversion_rate = (captured_orders / total_orders) if total_orders else 0.0
    result = {
        "gross_revenue": str(gross),
        "captured_revenue": str(captured),
        "total_orders": total_orders,
        "average_order_value": str(aov),
        "conversion_rate": conversion_rate,
        "top_selling_skus": top_skus,
    }
    await cache_manager.set(cache_key, json.dumps(result), ex=60)
    return result
