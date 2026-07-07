"""
ai_extract.py — server-side call to Gemini for reading an invoice / e-way bill /
weighbridge slip and pulling out structured fields.

Doing this on the backend (instead of straight from the browser, as the
earlier demo did) means the API key never has to live in client-side code,
and this now works in any browser, not just inside Claude.ai.
"""

import base64
import json
import os
import re
import io
import time
import asyncio

import httpx
from PyPDF2 import PdfReader

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
)

SYSTEM_PROMPT = (
    "You are an expert at extracting structured data from recycling/waste-management invoices, "
    "e-way bills, and weighbridge slips. Extract ALL visible fields carefully.\n\n"
    "Respond with ONLY a raw JSON object — no markdown formatting, no code fences, "
    "no commentary before or after it.\n\n"
    "Required fields:\n"
    "- invoice_no: The invoice, bill, or slip number (string or null if completely absent)\n"
    "- date: The document date in YYYY-MM-DD format (string or null if completely absent)\n"
    "- quantity_kg: Weight/quantity in kilograms (number >= 0, or null if completely absent)\n"
    "- rate_per_kg: Rate per kilogram in currency units (number >= 0, or null if completely absent)\n"
    "- facility_name: The facility, company, or transporter name (string or null if completely absent)\n\n"
    "CRITICAL: If a field is ambiguous or partially visible, make your best inference from the context. "
    "Only set to null if the field is COMPLETELY absent or the document is unintelligible for that field. "
    "Common variations: 'Invoice' = invoice_no, 'Invoice Date' = date, 'Weight' or 'Qty' = quantity_kg, "
    "'Unit Price' or 'Rate' = rate_per_kg, 'Consignee' or 'Company Name' = facility_name."
)


class ExtractionError(Exception):
    pass


def split_pdf_pages(file_bytes: bytes) -> list[bytes]:
    """Split a PDF into individual page PDFs. Returns list of PDF bytes, one per page."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for i in range(len(reader.pages)):
            writer = PdfReader()
            writer._pages = [reader.pages[i]]
            
            # Write page to bytes
            out = io.BytesIO()
            writer.write(out)
            pages.append(out.getvalue())
        return pages if pages else []
    except Exception:
        # If PDF splitting fails, return original file as single "page"
        return [file_bytes]


async def _call_gemini(file_bytes: bytes, media_type: str, api_key: str, retry_count: int = 0) -> dict:
    """Single call to Gemini API with retry logic."""
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": media_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
                {"text": "Extract the fields from this document as instructed. Be thorough and inferential — do not default to null."},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 1000,
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GEMINI_API_URL,
            params={"key": api_key},
            headers={"content-type": "application/json"},
            json=body,
        )

    if resp.status_code == 429:
        if retry_count < 2:
            await asyncio.sleep(2 ** retry_count)  # Exponential backoff
            return await _call_gemini(file_bytes, media_type, api_key, retry_count + 1)
        raise ExtractionError(
            "Gemini API quota exceeded — check your plan and billing at "
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        )
    
    if resp.status_code != 200:
        raise ExtractionError(
            f"Gemini API request failed ({resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise ExtractionError("No content returned by the model.")

    parts = candidates[0].get("content", {}).get("parts") or []
    text_block = next((p for p in parts if p.get("text")), None)
    if not text_block:
        raise ExtractionError("No text content returned by the model.")

    cleaned = re.sub(r"```json|```", "", text_block["text"]).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Could not parse the model's response as JSON: {e}")


async def extract_fields(file_bytes: bytes, content_type: str) -> dict:
    """Extract fields from a document, with multi-page PDF support."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ExtractionError(
            "No GEMINI_API_KEY is set on the server. Add one to your .env file to enable "
            "AI document reading (see README.md) — manual entry still works without it."
        )

    media_type = content_type or "application/octet-stream"
    
    # If it's a PDF, split into pages and extract from first page
    if media_type == "application/pdf":
        pages = split_pdf_pages(file_bytes)
        if pages:
            # Extract from first page
            return await _call_gemini(pages[0], media_type, api_key)
    
    # For non-PDFs or single-page PDFs, extract directly
    return await _call_gemini(file_bytes, media_type, api_key)
