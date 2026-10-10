import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Path, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, EmailStr
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup_event():
    # R10: Create indexes idempotently
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.employees.create_index("department")

# --------------------------------------------------------------------------- #
# Custom exception handler for 422 to match OpenAPI format exactly
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def truncate_ms(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def parse_ist_time(time_str: str):
    h, m = map(int, time_str.split(":"))
    return h, m

def get_attendance_date(punch_in_dt_ist: datetime, shift_start: str, shift_end: str) -> str:
    sh, sm = parse_ist_time(shift_start)
    eh, em = parse_ist_time(shift_end)
    is_overnight = (eh, em) <= (sh, sm)
    
    date_str = punch_in_dt_ist.strftime("%Y-%m-%d")
    if is_overnight:
        if (punch_in_dt_ist.hour, punch_in_dt_ist.minute, punch_in_dt_ist.second) < (eh, em, 0):
            prev_day = punch_in_dt_ist - timedelta(days=1)
            date_str = prev_day.strftime("%Y-%m-%d")
    return date_str

def get_late_minutes(punch_in_dt_ist: datetime, att_date: str, shift_start: str) -> int:
    y, m, d = map(int, att_date.split("-"))
    sh, sm = parse_ist_time(shift_start)
    shift_start_dt = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    
    diff_sec = (punch_in_dt_ist - shift_start_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min > 10 else 0

def get_overtime_minutes(punch_out_dt_ist: datetime, att_date: str, shift_end: str, shift_start: str) -> int:
    y, m, d = map(int, att_date.split("-"))
    sh, sm = parse_ist_time(shift_start)
    eh, em = parse_ist_time(shift_end)
    is_overnight = (eh, em) <= (sh, sm)
    
    shift_end_dt = datetime(y, m, d, eh, em, 0, tzinfo=IST)
    if is_overnight:
        shift_end_dt += timedelta(days=1)
        
    diff_sec = (punch_out_dt_ist - shift_end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0

def get_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours = Decimal(diff_sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def round_half_up(val: float, decimals: int = 2) -> float:
    return float(Decimal(val).quantize(Decimal(f"0.{'0'*decimals}"), rounding=ROUND_HALF_UP))

def to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def to_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(..., pattern=r'^\d{4}-\d{2}-\d{2}$')

class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="Database not ready")

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start and shift_end must differ")
    
    doc = body.model_dump()
    created_at_dt = truncate_ms(datetime.now(timezone.utc))
    doc["created_at"] = to_naive_utc(created_at_dt)
    
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    doc.pop("_id", None)
    return doc

@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    
    items = []
    for doc in cursor:
        doc.pop("_id", None)
        doc["created_at"] = to_epoch_ms(doc.get("created_at"))
        items.append(doc)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, detail="Invalid status for punch-in")
        
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_ms(ts)
    ts_ist = ts.astimezone(IST)
    
    att_date = get_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_min = get_late_minutes(ts_ist, att_date, emp["shift_start"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": to_naive_utc(ts),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
        
    doc.pop("_id", None)
    doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    doc["punch_out"] = None
    return doc

@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_ms(ts)
    ts_naive = to_naive_utc(ts)
    
    # Find the most recent record whose punch_in <= punched_at
    # Must explicitly match punch_out: None to handle concurrency
    log = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_naive}},
        sort=[("punch_in", -1)]
    )
    
    if not log:
        raise HTTPException(404, detail="No valid punch-in found")
        
    if log.get("punch_out") is not None:
        raise HTTPException(409, detail="Record is already punched out")
        
    pi_dt = to_aware_utc(log["punch_in"])
    if ts <= pi_dt:
        raise HTTPException(422, detail="punch_out must be strictly after punch_in")
    if (ts - pi_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")
        
    ts_ist = ts.astimezone(IST)
    work_hours = get_work_hours(pi_dt, ts)
    overtime_min = get_overtime_minutes(ts_ist, log["date"], emp["shift_end"], emp["shift_start"])
    half_day = work_hours < 4.50
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts_naive,
            "work_hours": work_hours,
            "overtime_minutes": overtime_min,
            "half_day": half_day
        }},
        return_document=True
    )
    
    if not updated:
        raise HTTPException(409, detail="Record was modified concurrently")
        
    updated.pop("_id", None)
    updated["punch_in"] = to_epoch_ms(updated["punch_in"])
    updated["punch_out"] = to_epoch_ms(updated["punch_out"])
    
    for h in updated.get("history", []):
        h["at"] = to_epoch_ms(h["at"])
        if "punch_in" in h["changes"]:
            h["changes"]["punch_in"]["from"] = to_epoch_ms(h["changes"]["punch_in"]["from"])
            h["changes"]["punch_in"]["to"] = to_epoch_ms(h["changes"]["punch_in"]["to"])
        if "punch_out" in h["changes"]:
            h["changes"]["punch_out"]["from"] = to_epoch_ms(h["changes"]["punch_out"]["from"])
            h["changes"]["punch_out"]["to"] = to_epoch_ms(h["changes"]["punch_out"]["to"])
            
    return updated

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from > date_to")
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]:
            raise HTTPException(422, detail="Invalid status")
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    
    items = []
    for doc in cursor:
        doc.pop("_id", None)
        doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
        doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
        for h in doc.get("history", []):
            h["at"] = to_epoch_ms(h["at"])
            if "punch_in" in h["changes"]:
                h["changes"]["punch_in"]["from"] = to_epoch_ms(h["changes"]["punch_in"]["from"])
                h["changes"]["punch_in"]["to"] = to_epoch_ms(h["changes"]["punch_in"]["to"])
            if "punch_out" in h["changes"]:
                h["changes"]["punch_out"]["from"] = to_epoch_ms(h["changes"]["punch_out"]["from"])
                h["changes"]["punch_out"]["to"] = to_epoch_ms(h["changes"]["punch_out"]["to"])
        items.append(doc)
        
    return {"items": items, "total": total, "page": page, "page_size": page_size}
