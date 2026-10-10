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
