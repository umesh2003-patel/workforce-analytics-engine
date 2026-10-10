# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submission Notes
I have successfully implemented all endpoints and requirements as per the `openapi.yaml` and `PROBLEM_STATEMENT.docx`.
- The full application logic resides in `app/main.py`.
- MongoDB unique indexes are created automatically at startup.
- All aggregations are performed natively in MongoDB (except where trivial Python calendar checks were simpler).
- Optimistic locking was used to ensure data consistency during concurrent patches/punch-outs.
- `DECISIONS.md` and `REVIEW.md` have been filled out with my rationale and observations.

Thank you for this interesting challenge!
