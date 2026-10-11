You are an expert Principal Backend Architect and Distributed Systems Engineer. 
Your task is to build a production-grade, asynchronous multi-tenant e-commerce and order management backend in Python 3.11+ using FastAPI, SQLAlchemy 2.0 (Async), PostgreSQL 16, and Redis 7.

Follow these strict design requirements and directory structure without skipping any implementation details.

### 1. Architectural & Domain Invariants
1. Multi-Tenancy (Shared DB, Discriminator Column):
   - Every domain table must contain `tenant_id` with compound indexes and constraints (e.g., `UniqueConstraint("tenant_id", "email")`, `UniqueConstraint("tenant_id", "sku")`).
   - Request context MUST be tracked per-request using Python's standard `contextvars.ContextVar` (`current_tenant_id`, `current_tenant_slug`, `is_platform_scope`).
   - Implement an ASGI middleware `tenant_resolution_middleware` that resolves tenant identity in order of priority:
     a. `X-Platform-Scope: true` (for platform administration)
     b. `X-Tenant-ID: <uuid>`
     c. `X-Tenant-Slug: <slug>`
     d. Host header subdomain (e.g. `merchant.platform.com`)
   - The resolved context must propagate safely across asynchronous coroutines without passing `tenant_id` into route business signatures.

2. High-Concurrency Flash Sale Inventory Engine:
   - Checkout MUST prevent race conditions and overselling using pessimistic row locking:
     `SELECT ... FROM inventories WHERE tenant_id = :t_id AND product_id = :p_id FOR UPDATE`.
   - To eliminate cyclic database deadlocks under high concurrency, line items MUST be sorted by `product_id` prior to acquiring row locks.
   - If `available < requested`, abort transaction immediately with HTTP 409 Conflict.
   - On successful hold: decrement `available`, increment `reserved`, and set an order expiration timestamp (`expires_at = now + 15 min`).

3. Redis Cache-Aside & Dynamic Invalidation:
   - Keyspace must strictly isolate tenants: `tenant:{tenant_id}:{namespace}:{key_suffix}`.
   - Implement non-blocking namespace cache invalidation (`cache_manager.invalidate_tenant_namespace`) using `SCAN`.
   - Cache catalog reads for 120s; return diagnostic headers `X-Cache: HIT` or `X-Cache: MISS`.
   - Invalidate `products` and `analytics` namespaces on every inventory or order status mutation.

4. Resilient Payment Gateway Webhooks & Outbox Store:
   - Provide constant-time HMAC-SHA256 signature generation and verification (`hmac.compare_digest`).
   - Store incoming events in a `payment_transactions` table with a unique constraint on `event_id`.
   - Idempotent processing: if `event_id` exists, return HTTP 200 with `duplicate_ignored` and exit without side-effects.
   - On `payment_intent.succeeded`: transition order to `PAYMENT_CAPTURED`, decrement `reserved`.
   - On `payment_intent.payment_failed`: transition order to `CANCELLED`, decrement `reserved`, and restore `available` stock.

5. Background Inventory Reclamation Worker:
   - Background worker running periodically (every 30s) scanning for orders in `PENDING_PAYMENT` where `expires_at <= UTC now`.
   - Worker must lock rows (`FOR UPDATE`), restore reserved units to available stock, mark order `CANCELLED`, and invalidate Redis caches.

6. Scoped Real-Time Store Analytics:
   - Compute Gross Revenue, Captured/Settled Revenue, Total Orders, Average Order Value (AOV), and Conversion Rate using PostgreSQL aggregate queries (`func.sum`, `func.count`).
   - Join `OrderItem`, `Product`, and `Order` to rank Top-Selling SKUs by settled units and revenue.
   - Cache results in Redis for 60s; enforce RBAC so `CUSTOMER` roles receive HTTP 403 Forbidden.

---

### 2. Required Directory & File Structure
Generate each of the following files completely with zero truncated sections or placeholder comments:

├── .dockerignore
├── .env.example
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml
├── app/
│   ├── __init__.py
│   ├── database.py
│   ├── main.py
│   ├── models.py
│   ├── payment.py
│   ├── redis_client.py
│   ├── schemas.py
│   └── worker.py
├── frontend/
│   └── index.html
├── tests/
│   └── test_engine.py
├── ARCHITECTURE_WIKI.md
├── Dockerfile
├── LICENSE
├── README.md
├── docker-compose.yml
└── requirements.txt

---

### 3. Dependency Specs (`requirements.txt`)
- fastapi>=0.115.0
- uvicorn[standard]>=0.30.0
- pydantic>=2.8.0
- sqlalchemy[asyncio]>=2.0.32
- asyncpg>=0.29.0
- passlib[bcrypt]>=1.7.4
- pyjwt>=2.9.0
- redis>=5.0.8
- httpx>=0.27.0
- pytest>=8.3.2
- pytest-asyncio>=0.24.0

---

### 4. Automated Test Suite Requirements (`tests/test_engine.py`)
Implement 4 distinct asynchronous Pytest suites:
1. `test_full_system_isolation_and_concurrency`:
   - Provisions two isolated tenants (`alpha-corp`, `beta-tech`).
   - Verifies duplicate SKUs can exist across tenants without collision.
   - Fires 10 concurrent requests (`asyncio.gather`) against a product with 2 units of inventory.
   - Asserts exactly 2 return HTTP 201 and 8 return HTTP 409, with final stock settling at 0.
2. `test_phase_two_features_caching_categories_and_hold_reclamation`:
   - Asserts `X-Cache: MISS` on first read and `X-Cache: HIT` on second read.
   - Backdates order `expires_at` to simulate timeout and triggers hold reclamation job.
   - Verifies units are restored to available inventory.
3. `test_phase_three_payment_webhooks_and_idempotency`:
   - Verifies valid HMAC signatures pass and forged signatures return HTTP 401.
   - Tests duplicate event delivery returns `duplicate_ignored`.
   - Tests failed payment webhook restores stock.
4. `test_phase_four_store_analytics_and_reporting`:
   - Validates customer role is denied with HTTP 403.
   - Verifies gross vs. net captured revenue calculations and AOV.
   - Asserts Store B metrics remain 0 when Store A has captured sales (tenant isolation).

---

### 5. Docker & CI/CD Pipeline
- `Dockerfile`: Multi-stage build with Python 3.11-slim, compiling dependencies in `/install` builder stage, copying into runner stage with unprivileged user `appuser:1001`, and adding a container `HEALTHCHECK`.
- `docker-compose.yml`: Spins up PostgreSQL 16 and Redis 7 with container healthchecks.
- `.github/workflows/ci.yml`: Runs on push/PR, boots real postgres and redis services, runs pytest, and verifies docker build.


🛠️ Step-by-Step Prompting Sequence for Iterative Agents

If using an interactive or chunked coding assistant (like Claude Code, Aider, or Copilot CLI), send these prompt chunks in order:

Chunk 1: Foundation, Database & Models

"Generate requirements.txt, docker-compose.yml, app/database.py (with contextvars tenant context), and app/models.py (Tenant, User, Category, Product, Inventory, Order, OrderItem, PaymentTransaction models with compound tenant unique constraints)."

Chunk 2: Schemas, Redis Client & Webhook Cryptography

"Generate app/schemas.py, app/redis_client.py (tenant-scoped key caching & pattern invalidation), and app/payment.py (constant-time HMAC-SHA256 signature creation and verification)."

Chunk 3: Worker & FastAPI Route Handlers

"Generate app/worker.py (background reservation TTL cleaner) and app/main.py (ASGI tenant resolution middleware, JWT auth, pessimistic lock checkout with deadlock avoidance, webhook listener, and analytics aggregations)."

Chunk 4: Frontend Dashboard & Test Suite

"Generate frontend/index.html (single-file Tailwind dashboard with live flash-sale simulator, webhook trigger, and analytics ribbon) and tests/test_engine.py (4 async pytest suites verifying isolation, 10-caller concurrency, webhooks, and analytics)."

Chunk 5: Containerization, CI/CD & Portfolio Documentation

"Generate Dockerfile (multi-stage non-root build), .dockerignore, .github/workflows/ci.yml, and README.md (recruiter-ready with system diagrams, badges, and resume bullet points)."