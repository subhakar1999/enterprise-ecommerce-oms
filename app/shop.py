"""Single-store storefront, protected administration, and Razorpay checkout."""

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_session
from app.models import Inventory, InvoiceCounter, Order, OrderItem, OrderStatus, Product, ShopOrder, ShopProduct, Tenant
from app.redis_client import cache_manager
from app.schemas import CheckoutCreate, ProductCreate

router = APIRouter()
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
login_attempts: dict[str, list[float]] = {}
COUNTRIES = set("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())


def settings() -> dict:
    countries = json.loads(os.getenv("SHIPPING_RATES") or '{"IN":"0.00"}')
    rates = {key.upper(): Decimal(str(value)) for key, value in countries.items()}
    if not rates or any(key != "*" and key not in COUNTRIES for key in rates):
        raise RuntimeError("Shipping rates require ISO country codes or a '*' fallback")
    if any(not value.is_finite() or value < 0 or value > Decimal("999999.99") for value in rates.values()):
        raise RuntimeError("Shipping rates must be finite, nonnegative INR amounts")
    return {"name": os.getenv("STORE_NAME", "XYZ Store"), "currency": "INR",
            "seller_address": os.getenv("SELLER_ADDRESS", ""), "support_email": os.getenv("SUPPORT_EMAIL", ""),
            "shipping_rates": {key: str(value.quantize(Decimal("0.01"))) for key, value in rates.items()},
            "shipping_countries": sorted(COUNTRIES if "*" in rates else rates),
            "gst_registered": False,
            "payments_ready": bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET")
                                   and os.getenv("SELLER_ADDRESS") and os.getenv("SUPPORT_EMAIL")),
            "payment_test_mode": os.getenv("RAZORPAY_KEY_ID", "").startswith("rzp_test_")}


async def bootstrap_store() -> None:
    async with SessionLocal() as session:
        slug = os.getenv("STORE_SLUG", "xyz-store")
        if await session.scalar(select(Tenant).where(Tenant.slug == slug)) is None:
            session.add(Tenant(slug=slug, name=settings()["name"]))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # Another application replica provisioned it.


async def store_id() -> uuid.UUID:
    async with SessionLocal() as session:
        value = await session.scalar(select(Tenant.tenant_id).where(Tenant.slug == os.getenv("STORE_SLUG", "xyz-store")))
    if value is None:
        raise HTTPException(503, "Store has not been initialized")
    return value


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = os.getenv("PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    if origin != expected:
        raise HTTPException(403, "Request origin is not allowed")


def auth_secret() -> str:
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "Admin login has not been configured")
    return secret


def admin_required(request: Request) -> None:
    try:
        payload = jwt.decode(request.cookies.get("shop_admin", ""), auth_secret(), algorithms=["HS256"],
                             options={"require": ["exp", "sub", "aud"]}, audience="shop-admin")
        if payload["sub"] != "admin":
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, "Please sign in") from exc
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        same_origin(request)


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class CatalogProduct(ProductCreate):
    image_url: str = Field(default="", max_length=2048)
    active: bool = True

    @field_validator("image_url")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if value and (urlsplit(value).scheme != "https" or not urlsplit(value).netloc):
            raise ValueError("Image URL must use HTTPS")
        return value


class Customer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=160)
    email: str = Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str = Field(min_length=6, max_length=30, pattern=r"^[+0-9 ()-]+$")
    address: str = Field(min_length=5, max_length=500)
    city: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(pattern=r"^[A-Z]{2}$")

    @field_validator("country")
    @classmethod
    def validate_country(cls, value: str) -> str:
        if value not in COUNTRIES:
            raise ValueError("Use a valid ISO country code")
        return value


class ShopCheckout(CheckoutCreate):
    customer: Customer
    request_id: uuid.UUID
    access_token: str = Field(min_length=32, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class PaymentVerification(BaseModel):
    razorpay_order_id: str = Field(max_length=100, pattern=r"^order_[a-zA-Z0-9]+$")
    razorpay_payment_id: str = Field(max_length=100, pattern=r"^pay_[a-zA-Z0-9]+$")
    razorpay_signature: str = Field(pattern=r"^[a-f0-9]{64}$")


class FulfilmentUpdate(BaseModel):
    status: Literal["UNFULFILLED", "PROCESSING", "SHIPPED", "DELIVERED"]
    tracking_url: str = Field(default="", max_length=2048)

    @field_validator("tracking_url")
    @classmethod
    def validate_tracking(cls, value: str) -> str:
        return CatalogProduct.validate_image(value)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def expired(order: Order) -> bool:
    return order.expires_at is None or order.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)


async def invalidate(tenant_id: uuid.UUID) -> None:
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "products")
    await cache_manager.invalidate_tenant_namespace(str(tenant_id), "analytics")


@router.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(FRONTEND / "admin.html")


@router.get("/api/store")
async def store_settings():
    return settings()


@router.post("/api/admin/login")
async def login(payload: Login, request: Request, response: Response):
    same_origin(request)
    configured = os.getenv("ADMIN_PASSWORD", "")
    secret = auth_secret()
    if len(configured) < 12:
        raise HTTPException(503, "Set an admin password of at least 12 characters")
    now = time.monotonic()
    # Single-process throttle. Use ingress rate limiting for multiple replicas.
    host = request.client.host if request.client else "unknown"
    for key in list(login_attempts):
        login_attempts[key] = [value for value in login_attempts[key] if now - value < 300]
        if not login_attempts[key]:
            del login_attempts[key]
    attempts = login_attempts.setdefault(host, [])
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many sign-in attempts. Try again in five minutes.")
    attempts.append(now)
    if not hmac.compare_digest(hashlib.sha256(payload.password.encode()).digest(), hashlib.sha256(configured.encode()).digest()):
        raise HTTPException(401, "Incorrect password")
    login_attempts.pop(host, None)
    token = jwt.encode({"sub": "admin", "aud": "shop-admin", "exp": datetime.now(timezone.utc) + timedelta(hours=8)}, secret, algorithm="HS256")
    response.set_cookie("shop_admin", token, httponly=True, samesite="strict", max_age=28800,
                        secure=os.getenv("COOKIE_SECURE", "true").lower() == "true", path="/api/admin")
    return {"status": "signed_in"}


@router.post("/api/admin/logout", dependencies=[Depends(admin_required)])
async def logout(response: Response):
    response.delete_cookie("shop_admin", path="/api/admin")
    return {"status": "signed_out"}


async def catalog(session: AsyncSession, tenant_id: uuid.UUID, include_inactive=False) -> list[dict]:
    query = select(Product, Inventory, ShopProduct).join(Inventory, (Inventory.product_id == Product.product_id) & (Inventory.tenant_id == Product.tenant_id)).outerjoin(ShopProduct, ShopProduct.product_id == Product.product_id).where(Product.tenant_id == tenant_id).order_by(Product.name)
    if not include_inactive:
        query = query.where(Product.active.is_(True))
    return [{"product_id": str(product.product_id), "sku": product.sku, "name": product.name,
             "description": product.description, "category": product.category, "price": str(product.price),
             "available": inventory.available, "reserved": inventory.reserved, "active": product.active,
             "image_url": media.image_url if media else ""}
            for product, inventory, media in (await session.execute(query)).all()]


@router.get("/api/products")
async def public_products(session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    return await catalog(session, tenant_id)


@router.get("/api/admin/products", dependencies=[Depends(admin_required)])
async def admin_products(session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    return await catalog(session, tenant_id, True)


@router.post("/api/admin/products", status_code=201, dependencies=[Depends(admin_required)])
async def add_product(payload: CatalogProduct, session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    product = Product(tenant_id=tenant_id, **payload.model_dump(exclude={"image_url", "initial_stock"}))
    try:
        async with session.begin():
            session.add(product)
            await session.flush()
            session.add(Inventory(tenant_id=tenant_id, product_id=product.product_id, available=payload.initial_stock, reserved=0))
            session.add(ShopProduct(product_id=product.product_id, image_url=payload.image_url))
    except IntegrityError as exc:
        raise HTTPException(409, "This SKU already exists") from exc
    await invalidate(tenant_id)
    return {"product_id": str(product.product_id)}


@router.put("/api/admin/products/{product_id}", dependencies=[Depends(admin_required)])
async def edit_product(product_id: uuid.UUID, payload: CatalogProduct, session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    try:
        async with session.begin():
            product = await session.scalar(select(Product).where(Product.product_id == product_id, Product.tenant_id == tenant_id).with_for_update())
            if product is None:
                raise HTTPException(404, "Product not found")
            inventory = await session.scalar(select(Inventory).where(Inventory.product_id == product_id, Inventory.tenant_id == tenant_id).with_for_update())
            if payload.initial_stock + inventory.reserved > 2_147_483_647:
                raise HTTPException(422, "Available and reserved stock exceed the supported limit")
            for key, value in payload.model_dump(exclude={"initial_stock", "image_url"}).items():
                setattr(product, key, value)
            inventory.available = payload.initial_stock
            media = await session.get(ShopProduct, product_id)
            if media is None:
                session.add(ShopProduct(product_id=product_id, image_url=payload.image_url))
            else:
                media.image_url = payload.image_url
    except IntegrityError as exc:
        raise HTTPException(409, "This SKU already exists") from exc
    await invalidate(tenant_id)
    return {"status": "updated"}


async def razorpay_request(method: str, path: str, data: dict | None = None) -> dict:
    key, secret = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        raise HTTPException(503, "Online payments are not configured yet. Please contact the store.")
    try:
        async with httpx.AsyncClient(timeout=15, auth=(key, secret)) as client:
            response = await client.request(method, "https://api.razorpay.com/v1/" + path, json=data)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "The payment provider is unavailable. Please try again.") from exc


def checkout_response(order: Order, detail: ShopOrder) -> dict:
    return {"order_id": str(order.order_id), "gateway_order_id": detail.gateway_order_id,
            "amount": int(order.total_amount * 100), "currency": "INR", "key_id": os.getenv("RAZORPAY_KEY_ID", ""),
            "status": order.status.value, "expires_at": order.expires_at.isoformat() if order.expires_at else None}


async def checkout_retry(session: AsyncSession, payload: ShopCheckout, tenant_id: uuid.UUID) -> dict | None:
    existing = await session.scalar(select(ShopOrder).where(ShopOrder.request_id == payload.request_id))
    if existing is None:
        return None
    order = await session.get(Order, existing.order_id)
    if order.tenant_id != tenant_id or not hmac.compare_digest(existing.token_hash, token_hash(payload.access_token)):
        raise HTTPException(409, "Checkout request conflict")
    if order.status != OrderStatus.PENDING_PAYMENT or expired(order):
        raise HTTPException(409, "This checkout has already ended. Start a new checkout.")
    return checkout_response(order, existing)


@router.post("/api/checkout", status_code=201)
async def checkout(payload: ShopCheckout, session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    try:
        return await create_checkout(payload, session, tenant_id)
    except IntegrityError as exc:
        await session.rollback()
        async with session.begin():
            retry = await checkout_retry(session, payload, tenant_id)
            if retry is not None:
                return retry
        raise HTTPException(409, "Checkout request conflict") from exc


async def create_checkout(payload: ShopCheckout, session: AsyncSession, tenant_id: uuid.UUID):
    config = settings()
    if not config["payments_ready"]:
        raise HTTPException(503, "The store is not accepting online payments yet. Please contact the store.")
    rate = config["shipping_rates"].get(payload.customer.country, config["shipping_rates"].get("*"))
    if rate is None:
        raise HTTPException(422, "Delivery is not available to this country yet")
    quantities: dict[uuid.UUID, int] = {}
    for item in payload.items:
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    async with session.begin():
        retry = await checkout_retry(session, payload, tenant_id)
        if retry is not None:
            return retry
        products = {}
        inventories = {}
        for product_id in sorted(quantities, key=str):
            product = await session.scalar(select(Product).where(Product.product_id == product_id, Product.tenant_id == tenant_id, Product.active.is_(True)).with_for_update())
            if product is None:
                raise HTTPException(404, "A product is no longer available")
            if not inventories:
                # A simultaneous retry may have waited for the first product
                # lock. Check again before rejecting stock reserved by it.
                retry = await checkout_retry(session, payload, tenant_id)
                if retry is not None:
                    return retry
            inventory = await session.scalar(select(Inventory).where(Inventory.product_id == product_id, Inventory.tenant_id == tenant_id).with_for_update())
            if inventory is None or inventory.available < quantities[product_id]:
                raise HTTPException(409, f"Not enough stock for {product.name}")
            products[product_id], inventories[product_id] = product, inventory
        total = sum((products[key].price * quantity for key, quantity in quantities.items()), Decimal(rate))
        if total > Decimal("9999999999.99"):
            raise HTTPException(422, "Order total exceeds the supported limit")
        order = Order(tenant_id=tenant_id, total_amount=total, status=OrderStatus.PENDING_PAYMENT,
                      expires_at=datetime.now(timezone.utc) + timedelta(minutes=15))
        session.add(order)
        await session.flush()
        gateway = await razorpay_request("POST", "orders", {"amount": int(total * 100), "currency": "INR", "receipt": order.order_id.hex})
        detail = ShopOrder(order_id=order.order_id, request_id=payload.request_id, token_hash=token_hash(payload.access_token),
                           customer_json=payload.customer.model_dump_json(), seller_json=json.dumps({
                               "name": config["name"], "address": config["seller_address"], "email": config["support_email"]}),
                           gateway_order_id=gateway["id"], shipping_amount=Decimal(rate))
        session.add(detail)
        for key, quantity in quantities.items():
            inventories[key].available -= quantity
            inventories[key].reserved += quantity
            session.add(OrderItem(tenant_id=tenant_id, order_id=order.order_id, product_id=key,
                                  sku=products[key].sku, quantity=quantity, unit_price=products[key].price))
    await invalidate(tenant_id)
    return checkout_response(order, detail)


async def authorized_order(session: AsyncSession, tenant_id: uuid.UUID, order_id: uuid.UUID, token: str) -> tuple[Order, ShopOrder]:
    result = (await session.execute(select(Order, ShopOrder).join(ShopOrder).where(Order.order_id == order_id, Order.tenant_id == tenant_id))).first()
    if result is None or not hmac.compare_digest(result[1].token_hash, token_hash(token)):
        raise HTTPException(404, "Order not found")
    return result


async def order_summary(session: AsyncSession, order: Order, detail: ShopOrder) -> dict:
    rows = (await session.execute(
        select(OrderItem, Product.name, ShopProduct.image_url)
        .outerjoin(Product, (Product.product_id == OrderItem.product_id) & (Product.tenant_id == OrderItem.tenant_id))
        .outerjoin(ShopProduct, ShopProduct.product_id == OrderItem.product_id)
        .where(OrderItem.order_id == order.order_id, OrderItem.tenant_id == order.tenant_id)
        .order_by(OrderItem.sku)
    )).all()
    return {"order_id": str(order.order_id), "status": order.status.value, "total": str(order.total_amount),
            "shipping": str(detail.shipping_amount), "currency": "INR", "created_at": order.created_at.isoformat(),
            "fulfilment": detail.fulfilment, "tracking_url": detail.tracking_url,
            "invoice_number": detail.invoice_number, "paid_at": detail.paid_at.isoformat() if detail.paid_at else None,
            "customer": json.loads(detail.customer_json), "seller": json.loads(detail.seller_json),
            "items": [{"product_id": str(item.product_id), "sku": item.sku, "name": name or item.sku,
                       "image_url": image_url or "", "quantity": item.quantity, "price": str(item.unit_price)}
                      for item, name, image_url in rows]}


@router.get("/api/orders/{order_id}")
async def read_order(order_id: uuid.UUID, x_order_token: str = Header(default=""), session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    order, detail = await authorized_order(session, tenant_id, order_id, x_order_token)
    return await order_summary(session, order, detail)


async def settle_payment(session: AsyncSession, tenant_id: uuid.UUID, gateway_order_id: str, payment: dict) -> uuid.UUID:
    async with session.begin():
        detail = await session.scalar(select(ShopOrder).join(Order).where(
            ShopOrder.gateway_order_id == gateway_order_id, Order.tenant_id == tenant_id).with_for_update())
        if detail is None:
            raise HTTPException(404, "Order not found")
        order = await session.scalar(select(Order).where(Order.order_id == detail.order_id).with_for_update())
        if payment.get("order_id") != gateway_order_id or payment.get("amount") != int(order.total_amount * 100) or payment.get("currency") != "INR" or payment.get("status") != "captured":
            raise HTTPException(409, "Payment has not been captured for this order")
        if order.status == OrderStatus.PAYMENT_CAPTURED:
            if detail.gateway_payment_id != payment["id"]:
                raise HTTPException(409, "Payment conflict")
            return order.order_id
        if order.status == OrderStatus.CANCELLED:
            # A late capture must be reviewed/refunded; never consume stock from
            # a hold already reclaimed by the worker.
            raise HTTPException(409, "Payment arrived after cancellation. Store support must review it.")
        items = await session.scalars(select(OrderItem).where(OrderItem.order_id == order.order_id).order_by(OrderItem.product_id))
        for item in items:
            inventory = await session.scalar(select(Inventory).where(Inventory.tenant_id == tenant_id, Inventory.product_id == item.product_id).with_for_update())
            inventory.reserved -= item.quantity
        detail.gateway_payment_id = payment["id"]
        paid_at = datetime.now(timezone.utc)
        india_date = paid_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
        year = india_date.year if india_date.month >= 4 else india_date.year - 1
        financial_year = f"{year}-{str(year + 1)[-2:]}"
        counter = await session.scalar(select(InvoiceCounter).where(
            InvoiceCounter.tenant_id == tenant_id, InvoiceCounter.financial_year == financial_year).with_for_update())
        if counter is None:
            try:
                async with session.begin_nested():
                    session.add(InvoiceCounter(tenant_id=tenant_id, financial_year=financial_year, last_number=0))
                    await session.flush()
            except IntegrityError:
                pass  # Another capture created this year's counter.
            counter = await session.scalar(select(InvoiceCounter).where(
                InvoiceCounter.tenant_id == tenant_id, InvoiceCounter.financial_year == financial_year).with_for_update())
        counter.last_number += 1
        detail.invoice_number = f"INV-{financial_year}-{counter.last_number:06d}"
        detail.paid_at = paid_at
        order.status, order.expires_at = OrderStatus.PAYMENT_CAPTURED, None
    await invalidate(tenant_id)
    return order.order_id


@router.post("/api/orders/{order_id}/verify")
async def verify_payment(order_id: uuid.UUID, payload: PaymentVerification, x_order_token: str = Header(default=""), session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    order, detail = await authorized_order(session, tenant_id, order_id, x_order_token)
    gateway_id = detail.gateway_order_id
    secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not secret:
        raise HTTPException(503, "Payments are not configured")
    expected = hmac.new(secret.encode(), f"{gateway_id}|{payload.razorpay_payment_id}".encode(), hashlib.sha256).hexdigest()
    if payload.razorpay_order_id != gateway_id or not hmac.compare_digest(expected, payload.razorpay_signature):
        raise HTTPException(401, "Payment signature is invalid")
    await session.rollback()
    payment = await razorpay_request("GET", "payments/" + payload.razorpay_payment_id)
    await settle_payment(session, tenant_id, gateway_id, payment)
    return {"status": "paid"}


@router.post("/api/payments/webhook")
async def webhook(request: Request, x_razorpay_signature: str = Header(default=""), session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(503, "Payment webhook is not configured")
    body = await request.body()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_razorpay_signature):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        event = json.loads(body)
        if not isinstance(event, dict):
            raise ValueError()
        if event.get("event") != "payment.captured":
            return {"status": "ignored"}
        payment = event["payload"]["payment"]["entity"]
        if not isinstance(payment, dict) or not isinstance(payment.get("id"), str) or not isinstance(payment.get("order_id"), str):
            raise ValueError()
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "Malformed payment event") from exc
    await settle_payment(session, tenant_id, payment["order_id"], payment)
    return {"status": "processed"}


@router.get("/api/orders/{order_id}/invoice", response_class=HTMLResponse)
@router.get("/api/orders/{order_id}/receipt", response_class=HTMLResponse, include_in_schema=False)
async def receipt(order_id: uuid.UUID, x_order_token: str = Header(default=""), session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    order, detail = await authorized_order(session, tenant_id, order_id, x_order_token)
    return await invoice_response(session, order, detail)


async def invoice_response(session: AsyncSession, order: Order, detail: ShopOrder) -> HTMLResponse:
    if order.status != OrderStatus.PAYMENT_CAPTURED:
        raise HTTPException(409, "Invoice is available after payment is captured")
    order_id = order.order_id
    data = await order_summary(session, order, detail)
    rows = "".join(f"<tr><td>{escape(item['sku'])}</td><td>{item['quantity']}</td><td>INR {item['price']}</td><td>INR {Decimal(item['price']) * item['quantity']:.2f}</td></tr>" for item in data["items"])
    seller, customer = data["seller"], data["customer"]
    paid_date = detail.paid_at.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y") if detail.paid_at else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>Invoice {escape(detail.invoice_number or '')}</title>
    <style>body{{font:16px system-ui;max-width:800px;margin:40px auto;padding:20px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #ddd}}.total{{font-size:24px}}</style></head><body>
    <h1>{escape(seller['name'])}</h1><p>{escape(seller['address'])}<br>{escape(seller['email'])}</p>
    <h2>Sales invoice</h2><p>Invoice: {escape(detail.invoice_number or '')}<br>Order: {order_id}<br>Date: {paid_date}</p>
    <p>Customer: {escape(customer['name'])}<br>{escape(customer['address'])}, {escape(customer['city'])}, {escape(customer['region'])}, {escape(customer['postal_code'])}, {escape(customer['country'])}</p>
    <table><thead><tr><th>Item</th><th>Quantity</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>
    <p>Subtotal: INR {order.total_amount - detail.shipping_amount:.2f}<br>Shipping: INR {data['shipping']}<br>GST charged: INR 0.00</p><p class="total">Paid: INR {data['total']}</p>
    <p>Payment reference: {escape(detail.gateway_payment_id or '')}<br>Payment received. Thank you for shopping with us.</p>
    <p>The seller is not GST-registered. This is a sales invoice, not a GST tax invoice.</p></body></html>""")


@router.get("/api/admin/orders/{order_id}/invoice", response_class=HTMLResponse, dependencies=[Depends(admin_required)])
async def admin_invoice(order_id: uuid.UUID, session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    row = (await session.execute(select(Order, ShopOrder).join(ShopOrder).where(
        Order.tenant_id == tenant_id, Order.order_id == order_id))).first()
    if row is None:
        raise HTTPException(404, "Order not found")
    return await invoice_response(session, *row)


@router.get("/api/admin/orders", dependencies=[Depends(admin_required)])
async def admin_orders(session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    rows = (await session.execute(select(Order, ShopOrder).join(ShopOrder).where(Order.tenant_id == tenant_id).order_by(Order.created_at.desc()).limit(200))).all()
    return [await order_summary(session, order, detail) for order, detail in rows]


@router.patch("/api/admin/orders/{order_id}", dependencies=[Depends(admin_required)])
async def fulfil_order(order_id: uuid.UUID, payload: FulfilmentUpdate, session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    async with session.begin():
        order = await session.scalar(select(Order).where(Order.tenant_id == tenant_id, Order.order_id == order_id).with_for_update())
        detail = await session.get(ShopOrder, order_id)
        if order is None or detail is None:
            raise HTTPException(404, "Order not found")
        if order.status != OrderStatus.PAYMENT_CAPTURED:
            raise HTTPException(409, "Only paid orders can be fulfilled")
        detail.fulfilment, detail.tracking_url = payload.status, payload.tracking_url
    return {"status": "updated"}


@router.get("/api/admin/summary", dependencies=[Depends(admin_required)])
async def admin_summary(session: AsyncSession = Depends(get_session), tenant_id=Depends(store_id)):
    revenue, paid = (await session.execute(select(func.coalesce(func.sum(Order.total_amount), 0), func.count()).where(Order.tenant_id == tenant_id, Order.status == OrderStatus.PAYMENT_CAPTURED))).one()
    pending = await session.scalar(select(func.count()).select_from(Order).where(Order.tenant_id == tenant_id, Order.status == OrderStatus.PENDING_PAYMENT))
    products = await session.scalar(select(func.count()).select_from(Product).where(Product.tenant_id == tenant_id))
    return {"revenue": str(revenue), "paid_orders": paid, "pending_orders": pending, "products": products}
