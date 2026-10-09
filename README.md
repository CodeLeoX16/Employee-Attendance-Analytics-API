# Employee Attendance & Analytics API

A robust, production-grade FastAPI and MongoDB backend service for HR attendance tracking, manual regularizations, and advanced analytics pipelines.

## Stack
* **Python 3.11+**
* **FastAPI**
* **MongoDB 6.0+ / PyMongo**
* **Pydantic v2**

## Local Setup & Running

The API requires a running MongoDB instance. Configuration is read from
`MONGO_URI` and `MONGO_DB`; if they are not set, the application uses
`mongodb://localhost:27017` and the `attendance_db` database.
For local configuration, copy `.env.example` to `.env`. Do not commit `.env`
or any file containing credentials or other secrets.

1. Create and activate a virtual environment:

   **Windows PowerShell**
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

   **macOS/Linux**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Start the development server from the project root:

   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

   The server will be available at `http://127.0.0.1:8000`.

## API Links

Use these links while the server is running:

| Resource | Link | Purpose |
|---|---|---|
| Health check | [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) | Confirms the API and MongoDB connection are available. A successful response is `{"status":"ok"}`. |
| Interactive Swagger UI | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) | Browse the endpoints, inspect schemas, and send test requests from your browser. |
| OpenAPI schema | [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json) | Returns the machine-readable API contract in JSON format. |
| ReDoc | [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc) | Provides an alternate, reference-style API documentation view. |

There is no `GET /` route, so opening
[http://127.0.0.1:8000/](http://127.0.0.1:8000/) directly returns `404 Not Found`.
Use the health check or one of the documentation links above to verify and
explore the service.

## Main Endpoint Groups

- **Employees:** `POST /employees`, `GET /employees`
- **Attendance:** `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- **Regularization:** `PATCH /attendance/{emp_code}/{date}`
- **Analytics:** `/analytics/...`
- **Administration:** `/admin/explain/{endpoint}`

For request parameters, response formats, status codes, and business rules,
refer to the [Swagger UI](http://127.0.0.1:8000/docs) or the
[OpenAPI schema](http://127.0.0.1:8000/openapi.json).