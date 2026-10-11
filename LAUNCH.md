# Launching the store

The customer-facing store and protected admin area are implemented. This is a
single-store application for a business based in India, with INR prices and
configurable delivery countries. It has not been deployed or tested against a
real merchant account yet.

## Merchant configuration

Set the store name, seller address, support email, admin password, and session
secret in your environment. Keep `.env` out of source control. Configure a
domain and HTTPS; set `PUBLIC_URL` to that exact origin and `COOKIE_SECURE=true`.
Admin sessions expire after eight hours. The application throttles login
attempts in one process; configure additional rate limits at the ingress.

Add your Razorpay key ID, secret, and a separate webhook secret. Configure
automatic capture in the merchant dashboard. Set the webhook URL to
`https://your-domain/api/payments/webhook` and subscribe to `payment.captured`.
The server verifies the raw webhook signature, order identity, amount,
currency, and captured status. Never put the secret keys in frontend code.

Integration references: [Razorpay Standard Checkout](https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/integration-steps/)
and [webhook validation](https://razorpay.com/docs/webhooks/validate-test/).

Test with Razorpay test credentials before using live credentials. Test success,
failure, cancellation, duplicate webhooks, interrupted browser sessions, and
late payments. A captured payment arriving after the hold has already been
cancelled returns a conflict and requires merchant review/refund through the
Razorpay dashboard; it does not consume another customer's stock.

Razorpay international payments need approval/activation on the merchant
account. Delivery availability is independent: set `SHIPPING_RATES` to an INR
fee per ISO country code, for example `{"IN":"80.00","US":"1500.00"}`. A `"*"`
fee permits other countries. Only enable countries your shipping arrangements
support. These are flat rates, not live carrier quotes.
See [Razorpay international payments](https://github.com/razorpay/markdown-docs/blob/master/payments/international-payments.md)
for merchant activation requirements.

## Billing and business policies

The customer confirmed that the business is not GST-registered. The application
generates a numbered sales invoice after verified payment, with GST charged as
zero. It includes seller/customer details, items, delivery charges, total paid,
and payment reference. Numbering is sequential per store and Indian financial
year; duplicate callbacks reuse the same invoice. Customers download HTML and
can open it to print/save as PDF. **It is not a GST tax invoice.**

Fill in the actual seller details before launch. If the business becomes
GST-registered, its tax calculation and invoice format need a separate update.
Product-specific tax rates, HSN/SAC, GSTIN, and export-specific billing/customs
documents are not implemented. Prices are charged exactly as configured;
the system does not add GST or other destination taxes.

Supply the actual customer support, shipping, returns/refunds, privacy, and
terms policies before launch. Refunds currently use the merchant dashboard;
automatic refund reconciliation and return management are not implemented.
Transactional email and cross-device customer accounts are not implemented.
Guest order access is saved in the customer's browser using a private token.
Store support can find orders by the customer information in the admin area.

## Database and deployment

Use PostgreSQL in production. SQLite is used only by local browser fixtures and
unit tests. The new `shop_products` and `shop_orders` tables extend the existing
inventory engine without dropping or changing existing tables. Back up an
existing database before upgrading.

`python -m app.init_db` creates missing tables and the configured store. This
is an additive first-install/upgrade command, not a general migration system.
Future table/column changes need reviewed migrations.

Use a separate Compose project with production environment variables:

```sh
docker compose -p xyz-production -f docker-compose.yml -f compose.production.yml build
docker compose -p xyz-production -f docker-compose.yml -f compose.production.yml up -d postgres redis
docker compose -p xyz-production -f docker-compose.yml -f compose.production.yml run --rm api python -m app.init_db
docker compose -p xyz-production -f docker-compose.yml -f compose.production.yml up -d --wait api
```

Set `POSTGRES_PASSWORD` and a matching `DATABASE_URL` using the hostname
`postgres` (for example `postgresql+asyncpg://oms:<URL-encoded-password>@postgres:5432/oms`).
The production override removes public database/cache ports and exposes the API
only on the host's loopback interface for an HTTPS reverse proxy. It requires
credentials and disables automatic schema creation. Verify this configuration
on the selected hosting service before deploying.

Configure database backups and verify a restore. Monitor application logs,
payment/webhook failures, and worker health. `/health` reports process liveness;
it is not a dependency-readiness probe. Review access control, secrets, and
backup handling before accepting customer data.

## Verification performed

Backend storefront tests run with SQLite and a mocked Razorpay HTTP adapter.
The browser fixture exercises the real frontend and backend with simulated
payments. Neither test makes a real charge. PostgreSQL concurrency tests remain
in `tests/test_engine.py` and need PostgreSQL/Redis services. Live gateway,
deployment, billing/business settings, shipping, email, and backup verification remain
required before this can be called production-ready.
