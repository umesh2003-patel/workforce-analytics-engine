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
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0

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

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r'^\d{4}-\d{2}-\d{2}$'),
    body: RegularizeRequest = None
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not log:
        raise HTTPException(404, detail="Record not found")
        
    changes = {}
    updates = {}
    
    new_status = body.status if body.status else log["status"]
    
    pi_val = body.punch_in if body.punch_in is not None else to_epoch_ms(log["punch_in"])
    po_val = body.punch_out if body.punch_out is not None else to_epoch_ms(log["punch_out"])
    
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch times for ABSENT or LEAVE")
        pi_val = None
        po_val = None
    else:
        if pi_val is None:
            raise HTTPException(422, detail="Presence status requires a punch_in")
            
    # Validate punch times
    pi_dt = None
    po_dt = None
    
    if pi_val is not None:
        pi_dt = datetime.fromtimestamp(pi_val / 1000, tz=timezone.utc)
        pi_dt = truncate_ms(pi_dt)
        # Verify punch_in stays on the record's attendance date
        pi_ist = pi_dt.astimezone(IST)
        derived_date = get_attendance_date(pi_ist, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(422, detail="punch_in does not fall on the record's date")
            
    if po_val is not None:
        if pi_val is None:
            raise HTTPException(422, detail="Cannot have punch_out without punch_in")
        po_dt = datetime.fromtimestamp(po_val / 1000, tz=timezone.utc)
        po_dt = truncate_ms(po_dt)
        if po_dt <= pi_dt:
            raise HTTPException(422, detail="punch_out must be after punch_in")
        if (po_dt - pi_dt).total_seconds() > 24 * 3600:
            raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")
            
    new_work_hours = None
    new_late = 0
    new_overtime = 0
    new_half = False
    
    if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
        pi_ist = pi_dt.astimezone(IST)
        new_late = get_late_minutes(pi_ist, date, emp["shift_start"])
        if po_dt:
            new_work_hours = get_work_hours(pi_dt, po_dt)
            po_ist = po_dt.astimezone(IST)
            new_overtime = get_overtime_minutes(po_ist, date, emp["shift_end"], emp["shift_start"])
            new_half = new_work_hours < 4.50
            
    # Check what changed
    def c(key, old, new):
        if old != new:
            changes[key] = {"from": old, "to": new}
            
    c("status", log["status"], new_status)
    c("punch_in", to_epoch_ms(log["punch_in"]), pi_val)
    c("punch_out", to_epoch_ms(log["punch_out"]), po_val)
    c("work_hours", log["work_hours"], new_work_hours)
    c("late_minutes", log.get("late_minutes", 0), new_late)
    c("overtime_minutes", log.get("overtime_minutes", 0), new_overtime)
    c("half_day", log.get("half_day", False), new_half)
    
    if not changes:
        raise HTTPException(422, detail="No changes made")
        
    # Translate changes for mongo (dates)
    mongo_changes = {}
    for k, v in changes.items():
        if k in ["punch_in", "punch_out"]:
            f = to_naive_utc(datetime.fromtimestamp(v["from"]/1000, tz=timezone.utc)) if v["from"] else None
            t = to_naive_utc(datetime.fromtimestamp(v["to"]/1000, tz=timezone.utc)) if v["to"] else None
            mongo_changes[k] = {"from": f, "to": t}
        else:
            mongo_changes[k] = {"from": v["from"], "to": v["to"]}
            
    history_entry = {
        "at": to_naive_utc(truncate_ms(datetime.now(timezone.utc))),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": mongo_changes
    }
    
    updates = {
        "status": new_status,
        "punch_in": to_naive_utc(pi_dt) if pi_dt else None,
        "punch_out": to_naive_utc(po_dt) if po_dt else None,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half
    }
    
    history_len = len(log.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"], "history": {"$size": history_len}},
        {"$set": updates, "$push": {"history": history_entry}},
        return_document=True
    )
    
    if not updated:
        raise HTTPException(409, detail="Concurrent modification")
        
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

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    y, m = map(int, month.split('-'))
    import calendar
    _, days_in_month = calendar.monthrange(y, m)
    working_days = 0
    joined_on_dt = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    
    for d in range(1, days_in_month + 1):
        dt = datetime(y, m, d).date()
        if dt >= joined_on_dt and dt.weekday() < 5:
            working_days += 1
            
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {"$addFields": {
            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_present", {"$in": ["$day_of_week", [2,3,4,5,6]]}]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        stats = {
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }
    else:
        stats = res[0]
        
    att_pct = None
    if working_days > 0:
        att_pct = round_half_up((stats["present_days"] / working_days) * 100, 2)
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(stats["present_days"]),
        "leave_days": int(stats["leave_days"]),
        "late_count": int(stats["late_count"]),
        "total_late_minutes": int(stats["total_late_minutes"]),
        "total_overtime_minutes": int(stats["total_overtime_minutes"]),
        "attendance_pct": att_pct
    }

@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    import calendar
    y, m = map(int, month.split('-'))
    _, days = calendar.monthrange(y, m)
    end_of_month = f"{month}-{days:02d}"
    
    pipeline = [
        {"$match": {"joined_on": {"$lte": end_of_month}}}
    ]
    if department:
        pipeline.append({"$match": {"department": department}})
        
    pipeline.extend([
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                    "date": {"$regex": f"^{month}"}
                }},
                {"$addFields": {
                    "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                }},
                {"$addFields": {
                    "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                    "present_val": {
                        "$cond": [
                            {"$and": ["$is_present", {"$in": ["$day_of_week", [2,3,4,5,6]]}]},
                            {"$cond": ["$half_day", 0.5, 1]},
                            0
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "all_logs": {"$push": "$logs"}
        }},
        {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
        {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$_id",
            "headcount": {"$first": "$headcount"},
            "present_days": {"$sum": "$all_logs.present_val"},
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$all_logs.late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$all_logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$all_logs.status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$all_logs.status", "ON_DUTY"]}, 1, 0]}
            },
            "work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$all_logs.is_present", {"$ne": ["$all_logs.work_hours", None]}]},
                        "$all_logs.work_hours",
                        0
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$all_logs.is_present", {"$ne": ["$all_logs.work_hours", None]}]},
                        1,
                        0
                    ]
                }
            }
        }},
        {"$sort": {"_id": 1}}
    ])
    
    res = list(db.employees.aggregate(pipeline))
    items = []
    for d in res:
        avg_wh = None
        if d["work_hours_count"] > 0:
            avg_wh = round_half_up(d["work_hours_sum"] / d["work_hours_count"], 2)
            
        items.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": float(d["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"],
        })
        
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"}
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
        
    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])
    
    res = list(db.attendance_logs.aggregate(pipeline))
    
    # Apply standard competition ranking
    items = []
    current_rank = 1
    current_late = -1
    for i, doc in enumerate(res):
        if i == 0:
            current_late = doc["total_late_minutes"]
        else:
            if doc["total_late_minutes"] < current_late:
                current_rank = i + 1
                current_late = doc["total_late_minutes"]
                
        if current_rank > limit:
            break
            
        doc["rank"] = current_rank
        items.append(doc)
        
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., alias="to", pattern=r'^\d{4}-\d{2}-\d{2}$')
):
    if from_date > to_date:
        raise HTTPException(422, detail="from > to")
    
    fd = datetime.strptime(from_date, "%Y-%m-%d")
    td = datetime.strptime(to_date, "%Y-%m-%d")
    if (td - fd).days > 92:
        raise HTTPException(422, detail="range exceeds 92 days")
        
    # Check if department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")
        
    # Use $densify to gap-fill within MongoDB
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            }
        }},
        {"$addFields": {
            "date_obj": {"$dateFromString": {"dateString": "$_id"}}
        }},
        {"$densify": {
            "field": "date_obj",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [fd, td + timedelta(days=1)]
            }
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "day_of_week": {"$dayOfWeek": "$date_obj"}
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"curr_date": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$curr_date"]}}},
                {"$count": "headcount"}
            ],
            "as": "hc"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date_obj": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$sort": {"date_obj": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    
    items = []
    for d in res:
        ar = None
        if d["attendance_rate"] is not None:
            ar = round_half_up(d["attendance_rate"], 4)
        mavg = None
        if d["moving_avg_7d"] is not None:
            mavg = round_half_up(d["moving_avg_7d"], 4)
            
        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": float(d["present_count"]),
            "late_count": d["late_count"],
            "attendance_rate": ar,
            "moving_avg_7d": mavg
        })
        
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
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
        explain_res = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }, verbosity="executionStats")
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="Missing required parameters")
            
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {"$addFields": {
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_present", {"$in": ["$day_of_week", [2,3,4,5,6]]}]},
                            {"$cond": ["$half_day", 0.5, 1]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }}
        ]
        
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="Missing month")
            
        import calendar
        y, m = map(int, month.split('-'))
        _, days = calendar.monthrange(y, m)
        end_of_month = f"{month}-{days:02d}"
        
        pipeline = [
            {"$match": {"joined_on": {"$lte": end_of_month}}}
        ]
        if department:
            pipeline.append({"$match": {"department": department}})
            
        pipeline.extend([
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                        "date": {"$regex": f"^{month}"}
                    }},
                    {"$addFields": {
                        "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                    }},
                    {"$addFields": {
                        "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                        "present_val": {
                            "$cond": [
                                {"$and": ["$is_present", {"$in": ["$day_of_week", [2,3,4,5,6]]}]},
                                {"$cond": ["$half_day", 0.5, 1]},
                                0
                            ]
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }},
            {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
            {"$unwind": {"path": "$all_logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {"$sum": "$all_logs.present_val"},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$all_logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$all_logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$all_logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$all_logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$all_logs.is_present", {"$ne": ["$all_logs.work_hours", None]}]},
                            "$all_logs.work_hours",
                            0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$all_logs.is_present", {"$ne": ["$all_logs.work_hours", None]}]},
                            1,
                            0
                        ]
                    }
                }
            }},
            {"$sort": {"_id": 1}}
        ])
        
        explain_res = db.command("explain", {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="Missing month")
            
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}"}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"}
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
            
        pipeline.extend([
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }}
        ])
        
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="Missing required parameters")
            
        fd = datetime.strptime(from_date, "%Y-%m-%d")
        td = datetime.strptime(to_date, "%Y-%m-%d")
        
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1]},
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }},
            {"$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$_id"}}
            }},
            {"$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [fd, td + timedelta(days=1)]
                }
            }},
            {"$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_obj"}
            }},
            {"$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$curr_date"]}}},
                    {"$count": "headcount"}
                ],
                "as": "hc"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"date_obj": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }},
            {"$sort": {"date_obj": 1}},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }}
        ]
        
        explain_res = db.command("explain", {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}
    else:
        raise HTTPException(422, detail="Unknown endpoint")
