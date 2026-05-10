# /healthz Endpoint Implementation Plan

**Goal:** Add a `/healthz` health-check endpoint to the FastAPI service and cover it with a unit test.

**Architecture:** A lightweight GET endpoint returning a static JSON payload is added directly to the existing FastAPI application instance in `app/main.py`. No new routers or dependencies are introduced. The test uses FastAPI's built-in `TestClient` (backed by `httpx`) for a fast, in-process integration check with no external services required.

## File Structure

- Modify: `app/main.py` — Add the `/healthz` route to the FastAPI application.
- Create: `tests/test_health.py` — Unit test asserting correct status code and response body.

## Tasks

---

### Task 1: Add the /healthz route

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the health-check endpoint to the FastAPI app**

```python
# app/main.py
from fastapi import FastAPI

app = FastAPI(title="My Service")


@app.get("/healthz", tags=["health"], summary="Health check")
async def healthz() -> dict:
    """Returns a simple JSON payload confirming the service is alive."""
    return {"status": "ok"}
```

> *Adds the `/healthz` GET route to the FastAPI application, returning `{"status": "ok"}` with HTTP 200.*

- [ ] **Step 2: Verify**

Run: `uvicorn app.main:app --port 8000 & sleep 1 && curl -s http://localhost:8000/healthz`

Expected: `{"status":"ok"}`

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "feat: add /healthz health-check endpoint"
```

---

### Task 2: Add a unit test for /healthz

**Files:**
- Create: `tests/test_health.py`

- [ ] **Step 1: Write the test module**

```python
# tests/test_health.py
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_returns_200():
    """GET /healthz must respond with HTTP 200."""
    response = client.get("/healthz")
    assert response.status_code == 200


def test_healthz_returns_ok_payload():
    """GET /healthz must return {"status": "ok"}."""
    response = client.get("/healthz")
    assert response.json() == {"status": "ok"}
```

> *Creates a unit test file that exercises the `/healthz` endpoint with FastAPI's `TestClient`, asserting both the HTTP status code and the JSON response body.*

- [ ] **Step 2: Verify**

Run: `pytest tests/test_health.py -v`

Expected:
```
tests/test_health.py::test_healthz_returns_200 PASSED
tests/test_health.py::test_healthz_returns_ok_payload PASSED

2 passed in 0.XXs
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_health.py
git commit -m "test: add unit tests for /healthz endpoint"
```