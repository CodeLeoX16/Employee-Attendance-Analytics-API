# REVIEW.md

List every defect found in the starter `app/main.py` (helpers and endpoints):

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` / helpers | Used naive datetimes without timezone conversion or proper IST shift start construction. | Fails on overnight shifts or when local machine clock differs from IST. | Built explicit IST-aware datetime baselines using `datetime(..., tzinfo=IST)`. |
| 2 | `compute_work_hours` / helpers | Used standard Python float division without enforcing round half-up (R4/R8). | Discrepancies in decimal precision against strict accounting tests. | Implemented custom `round_half_up` using `decimal.Decimal` with `ROUND_HALF_UP`. |
| 3 | `punch_in` endpoint | Check-then-insert pattern without handling concurrent race conditions. | Parallel punch-in requests created duplicate documents or threw unhandled server errors instead of 409. | Enforced a unique compound index on `(emp_code, date)` and caught PyMongo's `DuplicateKeyError` (11000) to return HTTP 409. |
| 4 | Startup lifecycle | No indexes were created at startup. | Slow performance on large collections and failure to enforce uniqueness rules. | Added a FastAPI `lifespan` handler to create all required indexes idempotently on startup. |
| 5 | `list_attendance` endpoint | Loaded entire collections into memory to sort and paginate in Python. | High memory consumption and severe performance degradation on a 100k-record dataset. | Pushed sorting (`.sort`), skipping, and limiting directly to the MongoDB query cursor. |
| 6 | Late calculation logic | Starter formula returned raw minutes without strictly enforcing the 10-minute grace rule check against shift start. | Incorrect late minutes assigned for punches right at the grace boundary. | Implemented rule R2 precisely: grace period 10 minutes, counting full elapsed minutes if strictly greater than 10 mins. |
| 7 | Missing endpoints | Entire contract for punch-out, regularization, and analytics pipelines was missing (`TODO`). | 404 or 500 errors when calling required business logic endpoints. | Implemented complete route handlers and native MongoDB aggregation pipelines. |

**Evaluated & Confirmed Fine:**
* **`POST /employees` validation structure:** The Pydantic `EmployeeIn` model structure correctly validates standard fields and patterns, though unique enforcement was reinforced at the database layer via unique indexes.
* **`GET /health` endpoint:** Basic readiness check is appropriate, though enhanced to perform an active MongoDB `.command("ping")` to accurately return 503 if the database drops.

## API verification notes

Start the service from the project root with:

```bash
uvicorn app.main:app --port 8000 --reload
```

The following URLs provide the supported ways to verify and explore the API:

- [Health check](http://127.0.0.1:8000/health) — returns `{"status":"ok"}` when the API and MongoDB are available.
- [Swagger UI](http://127.0.0.1:8000/docs) — interactive endpoint documentation and request testing.
- [OpenAPI schema](http://127.0.0.1:8000/openapi.json) — machine-readable API contract.
- [ReDoc](http://127.0.0.1:8000/redoc) — alternate reference documentation.

The application intentionally has no `GET /` route. Therefore,
[http://127.0.0.1:8000/](http://127.0.0.1:8000/) returning `404 Not Found` is
expected behavior and does not indicate that the server failed to start.