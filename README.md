# Sukha Goods (Demo Project)

> **Notice:** This repository is a **demo project** created to showcase an end-to-end e-commerce storefront and order management system (OMS) with high-concurrency stock reservation, guest checkout, payment verification, and administrative order fulfilment. It is not currently linked to an active merchant account.
>
> 📬 **Questions or Inquiries?** If you are interested in this project, would like a walkthrough, or want to discuss a custom build, feel free to **contact me**.

---

## What this demo showcases

- **Customer Storefront:** Product browsing, categories, live search, sorting, responsive mobile layout, and persistent shopping bag.
- **Guest Checkout:** Seamless customer order flow with ISO country delivery calculation and real-time stock reservation.
- **Payment Verification:** Razorpay gateway integration pattern with HMAC-SHA256 signed webhooks and simulated local checkout flow.
- **Admin Management Portal:** Dedicated administrative dashboard for product catalog management, real-time stock editing, order fulfilment, and delivery tracking updates.
- **Sequential Invoicing:** Automated Indian FY sequential sales invoice generation with downloadable HTML print receipts.
- **Engine Reliability:** Atomic inventory reservations with expiration cleanup and duplicate-payment protection.

---

## Quick local demo setup

### 1. Automated configuration
From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/configure_local.py
```

This generates `.env` with secure session secrets and sets the store branding to **Sukha Goods**.

### 2. Start the demo with Docker

```powershell
docker compose -f docker-compose.yml -f compose.local.yml up -d --build --wait
```

- **Storefront:** <http://127.0.0.1:8001>
- **Admin Portal:** <http://127.0.0.1:8001/admin> (Admin password is generated in `.env`)

To seed sample products and test orders for the demo:
```powershell
Get-Content scripts/seed_sample_products.py | docker compose exec -T api python -
Get-Content scripts/seed_sample_orders.py | docker compose exec -T api python -
```

### 3. Stop the demo

To stop the containers without wiping database state:

```powershell
docker compose -f docker-compose.yml -f compose.local.yml stop
```

To tear down all containers and networks:

```powershell
docker compose -f docker-compose.yml -f compose.local.yml down
```

---

## Running tests

### Unit & regression tests (No Docker needed)
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storefront.py tests/test_regressions.py -v
```

### Headless browser end-to-end smoke test
In one terminal, start the simulated test fixture:
```powershell
.\.venv\Scripts\python.exe scripts/browser_fixture.py
```

In another terminal:
```powershell
node scripts/browser_smoke.cjs
```

---

## Project Structure

- `frontend/`: Single-page storefront and admin management portal (HTML5, modern CSS, vanilla JavaScript).
- `app/shop.py`: Storefront API routes, admin authentication, checkout pipeline, and invoice generation.
- `app/main.py`: Application lifecycle and core OMS inventory engine routes.
- `app/models.py`: SQLAlchemy ORM database models (tenants, products, inventories, orders).
- `app/worker.py`: Background hold reclamation worker for expired unpaid reservations.
- `scripts/`: Local demo seed scripts, configuration generator, and browser test fixtures.

---

## Contact

If you have any questions about the architecture, implementation, or want to get in touch regarding this project, please feel free to reach out or open an issue!
