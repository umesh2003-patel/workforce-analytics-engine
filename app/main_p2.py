
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
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": log["_id"]},
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
