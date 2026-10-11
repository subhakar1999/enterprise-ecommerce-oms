"""Check a running store without creating products/orders or charging money."""
import json
import os
from pathlib import Path
import httpx
from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    base = os.getenv("OMS_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    checks = []
    with httpx.Client(base_url=base, timeout=15) as client:
        assert client.get("/health").json() == {"status": "ok"}
        checks.append("health")
        page = client.get("/")
        assert page.status_code == 200 and "shopping bag" in page.text
        checks.append("storefront")
        assert client.get("/admin").status_code == 200
        checks.append("admin page")
        response = client.get("/api/store")
        response.raise_for_status()
        store = response.json()
        response = client.get("/api/products")
        response.raise_for_status()
        products = response.json()
        checks.append("public catalog")
        assert client.get("/api/admin/orders").status_code in {401, 503}
        checks.append("admin access protection")
        password = os.getenv("ADMIN_PASSWORD")
        if password:
            response = client.post("/api/admin/login", json={"password": password}, headers={"Origin": base})
            response.raise_for_status()
            response = client.get("/api/admin/summary")
            response.raise_for_status()
            checks.append("admin login and summary")
            client.post("/api/admin/logout", headers={"Origin": base}).raise_for_status()
    print(json.dumps({"status": "passed", "base_url": base, "store": store["name"],
                      "products": len(products), "payments_ready": store["payments_ready"], "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
