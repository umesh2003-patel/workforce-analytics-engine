# Employee Attendance & Analytics API

This repository contains my submission for the HROne Software Engineer Trainee technical assignment.

It is a complete Employee Attendance and Analytics API built with **FastAPI** and **MongoDB**. The application enforces business rules for punch-ins/outs, handles manual corrections with an audit trail, and provides robust analytics endpoints driven by native MongoDB aggregation pipelines.

## Features Implemented
- **Full API Contract**: All endpoints defined in the OpenAPI specification are fully implemented.
- **Robust Analytics**: Aggregations (like department summaries, daily trends with gap-filling, and late leaderboards) are executed natively inside MongoDB pipelines for optimal performance.
- **Time & Concurrency**: Strict adherence to IST time zone rules, accurate overnight shift handling, and optimistic locking to prevent race conditions during concurrent punches or manual corrections.
- **Data Integrity**: Enforced through PyMongo-level compound unique indexes.

## Documentation
Please refer to the following files for my code review and technical rationale:
- `REVIEW.md`: My observations and fixes regarding the provided starter code.
- `DECISIONS.md`: The design decisions and optimizations made during implementation.

## Local Setup

**1. Create and activate a virtual environment:**
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .\.venv\Scripts\Activate.ps1
```

**2. Install dependencies:**
```bash
pip install -r requirements.txt
```

**3. Configure Environment Variables:**
Create a `.env` file in the root directory (do not commit this file) with your MongoDB credentials:
```env
MONGO_URI="mongodb+srv://<user>:<password>@cluster0..."
MONGO_DB="attendance_db"
```

**4. Seed Sample Data:**
```bash
python sample_seed.py
```

**5. Run the Server:**
```bash
uvicorn app.main:app --port 8000 --reload
```
The API documentation (Swagger UI) will be available at: http://127.0.0.1:8000/docs

---
*Thank you for the opportunity to work on this interesting challenge!*
