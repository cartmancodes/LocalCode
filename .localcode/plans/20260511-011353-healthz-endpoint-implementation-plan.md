# /healthz Endpoint Implementation Plan

**Goal:** Add a `/healthz` health-check endpoint to a FastAPI service that returns `{"status": "ok"}` with HTTP 200, covered by a pytest test.

**Architecture:** A single GET route is appended to the existing FastAPI application instance in `app/main.py`; no new routers or middleware are needed. The test uses FastAPI's built-in `TestClient` (which wraps `httpx`) to exercise the endpoint in-process without a live server. No new dependencies are required beyond `httpx` (already pulled in by `fastapi[testclient]`).

---

## File Structure

- **Modify:** `app/main.py` — FastAPI application; add the `/healthz` GET route
- **Create:** `tests/test_health.py` — pytest test module for the `/healthz` endpoint

---

## Tasks

### Task 1: Add the /healthz Route

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the /healthz endpoint to the FastAPI app**

  Open `app/main.py` and append the following route. The file must already contain (or have compatible equivalents of) the imports and `app` instance shown here. Add only the route function if `app` is already defined.

  ```python
  from fastapi import FastAPI

  app = FastAPI()


  @app.get("/healthz", status_code=200)
  def healthz() -> dict:
      """Liveness probe — returns 200 OK when the service is running."""
      return {"status": "ok"}
  ```

- [ ] **Step 2: Verify the server starts and the endpoint responds**

  Run: `uvicorn app.main:app --host 0.0.0.0 --port 8000 &; sleep 2 && curl -sf http://localhost:8000/healthz; kill %1`

  Expected:
  ```
  {"status":"ok"}
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add app/main.py
  git commit -m "feat: add /healthz liveness endpoint"
  ```

---

### Task 2: Add the pytest Test

**Files:**
- Create: `tests/test_health.py`

- [ ] **Step 1: Create the test file with a TestClient test**

  ```python
  import pytest
  from fastapi.testclient import TestClient

  from app.main import app

  client = TestClient(app)


  def test_healthz_returns_200_with_ok_status() -> None:
      """GET /healthz must return HTTP 200 and body {"status": "ok"}."""
      response = client.get("/healthz")

      assert response.status_code == 200
      assert response.json() == {"status": "ok"}
  ```

- [ ] **Step 2: Run the tests**

  Run: `pytest tests/test_health.py -v`

  Expected:
  ```
  tests/test_health.py::test_healthz_returns_200_with_ok_status PASSED
  1 passed in <1s
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add tests/test_health.py
  git commit -m "test: add pytest coverage for /healthz endpoint"
  ```