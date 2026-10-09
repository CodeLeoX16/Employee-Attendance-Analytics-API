"""
Employee Attendance & Analytics API - Complete Implementation
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Body
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def round_half_up(val: float, decimals: int = 2) -> float:
    """R1-R8: Strictly enforces half-up rounding."""
    d = Decimal(str(val))
    quant = Decimal('1.' + '0' * decimals) if decimals > 0 else Decimal('1')
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


def normalize_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    dt = normalize_utc(dt)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    # Validation per schema: epoch milliseconds range
    if not isinstance(ms, int) or ms < 100000000000 or ms > 4102444800000:
        raise HTTPException(422, "Invalid epoch milliseconds timestamp")
    # Truncate to whole seconds per R1
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent index creation at startup
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("department", 1), ("date", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helper Calculations (R1 - R5)
# --------------------------------------------------------------------------- #
def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in. Handles overnight shifts."""
    dt_ist = punch_in_utc.astimezone(IST)
    time_str = dt_ist.strftime("%H:%M")
    
    # Overnight shift check: shift_end <= shift_start
    if shift_end <= shift_start:
        # If punch-in is earlier than shift_end, it belongs to the previous day's shift
        if time_str < shift_end:
            dt_ist = dt_ist - timedelta(days=1)
            
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """R2: Grace period 10 minutes. Late if > shift_start + 10 mins."""
    dt_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    
    # Build shift start datetime in IST
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, tzinfo=IST)
    
    diff_seconds = (dt_ist - shift_start_dt).total_seconds()
    diff_minutes = int(diff_seconds // 60)
    
    if diff_minutes > 10:
        return diff_minutes
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds < 0:
        return 0.0
    return round_half_up(diff_seconds / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    """R3: Whole minutes from shift_end to punch_out if >= 30, else 0."""
    dt_ist = punch_out_utc.astimezone(IST)
    h, m = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    
    shift_end_dt = datetime(att_date.year, att_date.month, att_date.day, h, m, tzinfo=IST)
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)
        
    diff_minutes = int((dt_ist - shift_end_dt).total_seconds() // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Models & Request Validation
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT", pattern=r'^(PRESENT|WFH|ON_DUTY)$')


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r'^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$')
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start cannot equal shift_end")
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, max=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = to_epoch_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Punch-In & Punch-Out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = from_epoch_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    
    late_mins = compute_late_minutes(ts, emp["shift_start"], d)
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
        
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    doc["punch_out"] = None
    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = from_epoch_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
    ts = normalize_utc(ts)
    
    # Find most recent open record (punch_out is null) where punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(404, "No active punch-in found")
        
    p_in = normalize_utc(record["punch_in"])
    if ts <= p_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (ts - p_in).total_seconds() > 86400:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")
        
    wh = compute_work_hours(p_in, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    hd = compute_half_day(wh)
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": hd
        }},
        return_document=True
    )
    if not res:
        raise HTTPException(409, "Record already punched out")
        
    res.pop("_id", None)
    res["punch_in"] = to_epoch_ms(res["punch_in"])
    res["punch_out"] = to_epoch_ms(res["punch_out"])
    for h in res.get("history", []):
        h["at"] = to_epoch_ms(h["at"])
        for k, v in h["changes"].items():
            if k in ("punch_in", "punch_out"):
                v["from"] = to_epoch_ms(v["from"])
                v["to"] = to_epoch_ms(v["to"])
    return res


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, max=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be greater than date_to")
        
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    
    items = []
    for doc in cursor:
        doc.pop("_id", None)
        doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
        doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
        for h in doc.get("history", []):
            h["at"] = to_epoch_ms(h["at"])
            for k, v in h["changes"].items():
                if k in ("punch_in", "punch_out"):
                    v["from"] = to_epoch_ms(v["from"])
                    v["to"] = to_epoch_ms(v["to"])
        items.append(doc)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")
        
    # Determine new values
    new_status = body.status if body.status is not None else record["status"]
    
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot have punch times")
        new_p_in = None
        new_p_out = None
    else:
        # Presence status requires punch_in
        new_p_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else normalize_utc(record.get("punch_in"))
        if not new_p_in:
            raise HTTPException(422, "Presence status requires punch_in")
        new_p_in = normalize_utc(new_p_in)
        
        # Verify punch_in stays on attendance date per R1
        calc_d = get_attendance_date(new_p_in, emp["shift_start"], emp["shift_end"])
        if calc_d != date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")
            
        new_p_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else normalize_utc(record.get("punch_out"))
        if new_p_out is not None:
            new_p_out = normalize_utc(new_p_out)
            if new_p_out <= new_p_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_p_out - new_p_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out cannot exceed 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_p_in, emp["shift_start"], date)
        if new_p_out is not None:
            new_wh = compute_work_hours(new_p_in, new_p_out)
            new_ot = compute_overtime(new_p_out, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)
        else:
            new_wh = None
            new_ot = 0
            new_hd = False

    # Check what actually changed
    changes = {}
    fields_to_check = {
        "status": (record.get("status"), new_status),
        "punch_in": (record.get("punch_in"), new_p_in),
        "punch_out": (record.get("punch_out"), new_p_out),
        "work_hours": (record.get("work_hours"), new_wh),
        "late_minutes": (record.get("late_minutes", 0), new_late),
        "overtime_minutes": (record.get("overtime_minutes", 0), new_ot),
        "half_day": (record.get("half_day", False), new_hd),
    }
    
    for field, (old_val, new_val) in fields_to_check.items():
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}
            
    if not changes:
        raise HTTPException(422, "Request changes nothing")
        
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_p_in,
                "punch_out": new_p_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry}
        },
        return_document=True
    )
    
    if not updated:
        raise HTTPException(404, "Record not found")
        
    updated.pop("_id", None)
    updated["punch_in"] = to_epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = to_epoch_ms(updated.get("punch_out"))
    for h in updated.get("history", []):
        h["at"] = to_epoch_ms(h["at"])
        for k, v in h["changes"].items():
            if k in ("punch_in", "punch_out"):
                v["from"] = to_epoch_ms(v["from"])
                v["to"] = to_epoch_ms(v["to"])
    return updated


# --------------------------------------------------------------------------- #
# Endpoints: Analytics & Explain
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    y, m = map(int, month.split("-"))
    # Calculate working days (Mon-Fri) from joined_on to month end
    start_date = date(y, m, 1)
    if m == 12:
        end_date = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(y, m + 1, 1) - timedelta(days=1)
        
    joined_on = date.fromisoformat(emp["joined_on"])
    effective_start = max(start_date, joined_on)
    
    working_days = 0
    curr = effective_start
    while curr <= end_date:
        if curr.weekday() < 5: # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
        
    # Aggregate logs for the month
    month_prefix = month
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month_prefix}"}
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    data = res[0] if res else {"present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    
    present_days = data["present_days"]
    attendance_pct = round_half_up((present_days / working_days) * 100, 2) if working_days > 0 else None
    
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    if m == 12:
        month_end = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(y, m + 1, 1) - timedelta(days=1)
    month_end_str = month_end.isoformat()
    
    match_emp = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_emp["department"] = department
        
    pipeline = [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp"]},
                    "date": {"$regex": f"^{month}"}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$logs.half_day", 0.5, 1.0]},
                    0
                ]
            }},
            "total_work_hours": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$logs.work_hours", None]}
                    ]},
                    "$logs.work_hours",
                    0
                ]
            }},
            "work_hours_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$logs.work_hours", None]}
                    ]},
                    1,
                    0
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$work_hours_count", 0]},
                    {"$divide": ["$total_work_hours", "$work_hours_count"]},
                    None
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": ASCENDING}}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    for it in items:
        it.pop("_id", None)
        if it["avg_work_hours"] is not None:
            it["avg_work_hours"] = round_half_up(it["avg_work_hours"], 2)
            
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, max=50),
    department: Optional[str] = None
):
    match_emp = {}
    if department:
        match_emp["department"] = department
        
    pipeline = [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp"]},
                    "date": {"$regex": f"^{month}"},
                    "late_minutes": {"$gt": 0}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$name"},
            "department": {"$first": "$department"},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING, "_id": ASCENDING},
            "output": {
                "rank": {"$denseRank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ]
    
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str = Path(...), from_date: str = Query(..., alias="from", format="date"), to_date: str = Query(..., alias="to", format="date")):
    if from_date > to_date:
        raise HTTPException(422, "from cannot be greater than to")
        
    f_dt = date.fromisoformat(from_date)
    t_dt = date.fromisoformat(to_date)
    if (t_dt - f_dt).days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
        
    # Verify department exists
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")
        
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp"]},
                    "date": {"$gte": from_date, "$lte": to_date}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "emp_code": 1,
            "joined_on": 1,
            "date": "$logs.date",
            "status": "$logs.status",
            "half_day": "$logs.half_day",
            "late_minutes": "$logs.late_minutes"
        }},
        # We want a daily timeline. Let's use $densify or generate dates in python/aggregation.
        # Since MongoDB $densify requires a numeric or date field, let's group by date.
    ]
    
    # Generate all dates between from_date and to_date
    dates_list = []
    curr = f_dt
    while curr <= t_dt:
        dates_list.append(curr.isoformat())
        curr += timedelta(days=1)
        
    # Aggregation for daily metrics
    daily_pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp"]},
                    "date": {"$gte": from_date, "$lte": to_date}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # Project employee data and log data per day
    ]
    
    # Alternatively, compute cleanly via python aggregation join or specialized pipeline:
    # Let's collect employees and logs
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    logs = list(db.attendance_logs.find({
        "emp_code": {"$in": [e["emp_code"] for e in emps]},
        "date": {"$gte": from_date, "$lte": to_date}
    }))
    
    logs_map = {}
    for l in logs:
        logs_map.setdefault(l["date"], []).append(l)
        
    trend_items = []
    rate_history = []
    
    for d_str in dates_list:
        d_obj = date.fromisoformat(d_str)
        is_wd = d_obj.weekday() < 5
        
        # Headcount R9: joined_on <= d_str
        headcount = sum(1 for e in emps if e["joined_on"] <= d_str)
        
        day_logs = logs_map.get(d_str, [])
        present_count = 0.0
        late_count = 0
        
        for l in day_logs:
            if l.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if l.get("half_day") else 1.0
            if (l.get("late_minutes") or 0) > 0:
                late_count += 1
                
        if is_wd and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
            rate_history.append(att_rate)
        else:
            att_rate = None
            
        # 7-day moving average over preceding window inside requested range
        window = rate_history[-7:] if rate_history else []
        non_null_window = [r for r in window if r is not None]
        moving_avg = round_half_up(sum(non_null_window) / len(non_null_window), 4) if non_null_window else None
        
        trend_items.append({
            "date": d_str,
            "is_working_day": is_wd,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": att_rate,
            "moving_avg_7d": moving_avg
        })
        
    return {"department": department, "items": trend_items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20
):
    col_name = "attendance_logs"
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        col_name = "attendance_logs"
        match_q = {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}
        explain_res = db.attendance_logs.find(match_q).explain("executionStats")
        return {"endpoint": endpoint, "collection": col_name, "explain": explain_res}
        
    elif endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain_res = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": col_name, "explain": explain_res}
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        col_name = "employees"
        pipeline = [
            {"$match": {"joined_on": {"$lte": f"{month}-31"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$emp_code", "$$emp"]
                                },
                                "date": {
                                    "$regex": f"^{month}"
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            }
        ]
        explain_res = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": col_name, "explain": explain_res}
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        col_name = "employees"
        pipeline = [
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$emp_code", "$$emp"]
                                },
                                "date": {
                                    "$regex": f"^{month}"
                                },
                                "late_minutes": {
                                    "$gt": 0
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {"$unwind": "$logs"},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$logs.late_minutes"}}}
        ]
        explain_res = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": col_name, "explain": explain_res}
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to required")
        col_name = "employees"
        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$emp_code", "$$emp"]
                                },
                                "date": {
                                    "$gte": from_date,
                                    "$lte": to_date
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            }
        ]
        explain_res = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": col_name, "explain": explain_res}
        
    raise HTTPException(422, "Invalid endpoint")