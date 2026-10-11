import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=160)


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    tenant_id: uuid.UUID
    slug: str
    name: str


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str = Field(default="uncategorized", max_length=100)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    initial_stock: int = Field(default=0, ge=0, le=2_147_483_647)


class ProductRead(BaseModel):
    product_id: uuid.UUID
    sku: str
    name: str
    description: str
    category: str
    price: Decimal
    available: int


class CheckoutItem(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0, le=2_147_483_647)


class CheckoutCreate(BaseModel):
    items: list[CheckoutItem] = Field(min_length=1, max_length=100)


class PaymentEventData(BaseModel):
    order_id: uuid.UUID


class PaymentEvent(BaseModel):
    id: str = Field(min_length=1, max_length=200, pattern=r"\S")
    type: str
    data: PaymentEventData


class OrderRead(BaseModel):
    order_id: uuid.UUID
    status: str
    total_amount: Decimal
    expires_at: str | None


class OrderItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: uuid.UUID
    sku: str
    quantity: int
    unit_price: Decimal


class OrderSummary(BaseModel):
    order_id: uuid.UUID
    status: str
    total_amount: Decimal
    created_at: datetime
    expires_at: datetime | None
    items: list[OrderItemRead]


class AnalyticsRead(BaseModel):
    gross_revenue: Decimal
    captured_revenue: Decimal
    total_orders: int
    average_order_value: Decimal
    conversion_rate: float
    top_selling_skus: list[dict]
