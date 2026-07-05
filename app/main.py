"""
main.py — Pramaan backend.

Run locally with:  uvicorn app.main:app --reload --port 8000
Then open  http://localhost:8000  in any browser.

Deployed on Vercel, this same `app` object is the whole Vercel Function —
see /index.py at the project root, and DEPLOY_VERCEL.md for the how-to.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from app import db
from app.verify import run_verification
from app.ai_extract import extract_fields, ExtractionError
from app.pdf_report import build_pdf

load_dotenv()  # no-op if there's no .env file (e.g. on Vercel) — that's fine


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Pramaan", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # fine for a demo tool; tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class DocIn(BaseModel):
    invoice: str = ""
    date: str = ""
    qty: float = 0
    rate: float = 0


class VerifyRequest(BaseModel):
    name: str = ""
    period: str = ""
    claimed: float = 0
    capacity: float = 0
    rate_min: float = 0
    rate_max: float = 10**9
    docs: list[DocIn] = []


@app.post("/api/verify")
def verify_case(payload: VerifyRequest):
    with db.get_conn() as conn:
        record = run_verification(payload.model_dump(), conn, db)
        db.save_case(conn, record)
    return record


@app.get("/api/cases")
def get_cases():
    with db.get_conn() as conn:
        return db.list_cases(conn)


@app.get("/api/cases/{case_no}")
def get_case(case_no: str):
    with db.get_conn() as conn:
        case = db.get_case(conn, case_no)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/api/cases/{case_no}/pdf")
def get_case_pdf(case_no: str):
    with db.get_conn() as conn:
        case = db.get_case(conn, case_no)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    pdf_bytes = build_pdf(case)
    filename = f"pramaan_{case_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.delete("/api/cases")
def delete_all_cases():
    with db.get_conn() as conn:
        db.clear_all(conn)
    return {"ok": True}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    file_bytes = await file.read()
    try:
        fields = await extract_fields(file_bytes, file.content_type)
    except ExtractionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return fields


@app.get("/api/health")
def health():
    return {"ok": True, "ai_configured": bool(os.environ.get("GEMINI_API_KEY"))}


# Serve the frontend last so it doesn't shadow the /api/* routes above.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
