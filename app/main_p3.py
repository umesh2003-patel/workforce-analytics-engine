
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

