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
import asyncio
from datetime import datetime

import httpx
from PyPDF2 import PdfReader, PdfWriter

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
)

SYSTEM_PROMPT = (
    "You are an expert OCR system for Indian recycling/waste-management documents: "
    "tax invoices, e-way bills (EWBN), weighbridge slips, and delivery challans.\n\n"
    "Read the ENTIRE document — every page, table row, header, stamp, and handwritten note. "
    "Do not skip partially visible or low-contrast text.\n\n"
    "Return a JSON object with a single key `documents`: an array of one object per distinct "
    "invoice/bill/slip found. A multi-page PDF may contain one document spanning pages or "
    "several separate invoices — return one array entry per invoice.\n\n"
    "Each document object must have exactly these keys:\n"
    "- invoice_no: invoice / bill / challan / e-way bill (EWBN) number (string or null)\n"
    "- date: document date as YYYY-MM-DD (string or null)\n"
    "- quantity_kg: net weight or quantity in kilograms (number >= 0, or null)\n"
    "- rate_per_kg: price per kg in rupees, before GST if both are shown (number >= 0, or null)\n"
    "- facility_name: consignee, buyer, recycler, or facility name (string or null)\n\n"
    "Extraction rules:\n"
    "- Convert metric tonnes (MT, Ton, T) to kg by multiplying by 1000.\n"
    "- Parse Indian dates: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY → YYYY-MM-DD.\n"
    "- If only total amount and quantity are shown, compute rate_per_kg = amount / quantity_kg.\n"
    "- Map labels: 'Inv No', 'Bill No', 'EWBN', 'E-Way Bill No' → invoice_no; "
    "'Net Wt', 'Gross Wt', 'Qty', 'Weight' → quantity_kg; "
    "'Rate', 'Unit Price', 'Price/Kg' → rate_per_kg; "
    "'Consignee', 'Party Name', 'Buyer', 'To' → facility_name.\n"
    "- Strip currency symbols (₹, Rs.) and thousands separators from numbers.\n"
    "- Infer missing fields from context rather than returning null when text is present but unclear.\n"
    "- Only use null when a field is genuinely absent from the document."
)

DOCUMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "invoice_no": {"type": "STRING", "nullable": True},
        "date": {"type": "STRING", "nullable": True},
        "quantity_kg": {"type": "NUMBER", "nullable": True},
        "rate_per_kg": {"type": "NUMBER", "nullable": True},
        "facility_name": {"type": "STRING", "nullable": True},
    },
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "documents": {
            "type": "ARRAY",
            "items": DOCUMENT_SCHEMA,
        }
    },
    "required": ["documents"],
}

_INVOICE_KEYS = (
    "invoice_no", "invoice_number", "invoice", "inv_no", "bill_no",
    "document_no", "ewb_no", "eway_bill_no", "challan_no", "slip_no",
)
_DATE_KEYS = ("date", "invoice_date", "bill_date", "document_date", "eway_date")
_QTY_KEYS = (
    "quantity_kg", "quantity", "qty", "weight_kg", "weight",
    "net_weight", "net_weight_kg", "gross_weight_kg",
)
_RATE_KEYS = ("rate_per_kg", "rate", "unit_price", "price_per_kg", "price")
_FACILITY_KEYS = (
    "facility_name", "company_name", "consignee", "party_name",
    "buyer", "seller", "transporter", "recipient", "customer",
)
_AMOUNT_KEYS = ("total_amount", "amount", "total", "invoice_amount", "grand_total")


class ExtractionError(Exception):
    pass


def detect_media_type(file_bytes: bytes, content_type: str | None) -> str:
    """Infer MIME type from content when the browser sends a generic type."""
    if content_type and content_type not in ("application/octet-stream", ""):
        return content_type
    if file_bytes[:4] == b"%PDF":
        return "application/pdf"
    if file_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if file_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "image/webp"
    if file_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return content_type or "application/octet-stream"


def preprocess_image(file_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    """Fix EXIF rotation and upscale small photos for more reliable reading."""
    if not media_type.startswith("image/"):
        return file_bytes, media_type
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) < 1400:
            scale = 1400 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return file_bytes, media_type


def split_pdf_pages(file_bytes: bytes) -> list[bytes]:
    """Split a PDF into individual page PDFs. Returns list of PDF bytes, one per page."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            out = io.BytesIO()
            writer.write(out)
            pages.append(out.getvalue())
        return pages if pages else []
    except Exception:
        return [file_bytes]


def _first_present(raw: dict, keys: tuple[str, ...]):
    for key in keys:
        val = raw.get(key)
        if val is not None and val != "":
            return val
    return None


def _parse_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[₹Rs.\s]", "", text, flags=re.IGNORECASE)
    text = text.replace(",", "")
    mt_match = re.match(r"^([\d.]+)\s*(?:MT|MTS?|TON(?:NE)?S?)$", text, re.IGNORECASE)
    if mt_match:
        return float(mt_match.group(1)) * 1000
    try:
        num = float(text)
        return num if num >= 0 else None
    except ValueError:
        return None


def _parse_date(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _maybe_convert_tonnes(qty: float | None, raw: dict) -> float | None:
    if qty is None:
        return None
    raw_text = json.dumps(raw, default=str).lower()
    if qty <= 200 and re.search(r"\b(mt|mts?|ton|tonne|metric\s*ton)\b", raw_text):
        return qty * 1000
    tonnes = _parse_number(_first_present(raw, ("quantity_mt", "weight_mt", "tonnes", "weight_ton")))
    if tonnes is not None and qty <= 200:
        return tonnes * 1000
    return qty


def normalize_document(raw: dict) -> dict:
    """Coerce model output to a consistent shape with sane units and formats."""
    invoice = _first_present(raw, _INVOICE_KEYS)
    date = _parse_date(_first_present(raw, _DATE_KEYS))
    qty = _maybe_convert_tonnes(_parse_number(_first_present(raw, _QTY_KEYS)), raw)
    rate = _parse_number(_first_present(raw, _RATE_KEYS))
    facility = _first_present(raw, _FACILITY_KEYS)

    if rate is None and qty and qty > 0:
        amount = _parse_number(_first_present(raw, _AMOUNT_KEYS))
        if amount is not None:
            rate = round(amount / qty, 2)

    return {
        "invoice_no": str(invoice).strip() if invoice else None,
        "date": date,
        "quantity_kg": round(qty, 2) if qty is not None else None,
        "rate_per_kg": round(rate, 2) if rate is not None else None,
        "facility_name": str(facility).strip() if facility else None,
    }


def document_has_data(doc: dict) -> bool:
    return any(doc.get(k) is not None for k in ("invoice_no", "date", "quantity_kg", "rate_per_kg"))


def parse_documents_response(raw: dict | list) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    if isinstance(raw.get("documents"), list):
        return raw["documents"]
    return [raw]


def _dedupe_documents(documents: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique = []
    for doc in documents:
        key = (doc.get("invoice_no") or "", doc.get("date") or "", doc.get("quantity_kg") or "")
        if key in seen and key != ("", "", ""):
            continue
        seen.add(key)
        unique.append(doc)
    return unique


def _is_sparse(documents: list[dict], page_count: int) -> bool:
    if page_count <= 1:
        return False
    populated = [d for d in documents if document_has_data(d)]
    if not populated:
        return True
    if len(populated) < page_count:
        best = max(populated, key=lambda d: sum(1 for k in ("invoice_no", "date", "quantity_kg", "rate_per_kg") if d.get(k) is not None))
        if sum(1 for k in ("invoice_no", "date", "quantity_kg", "rate_per_kg") if best.get(k) is not None) < 3:
            return True
    return False


def format_api_response(documents: list[dict]) -> dict:
    if not documents:
        raise ExtractionError("Could not extract any fields from this document.")
    primary = documents[0]
    result = dict(primary)
    if len(documents) > 1:
        result["documents"] = documents
    return result


async def _call_gemini(
    file_bytes: bytes,
    media_type: str,
    api_key: str,
    *,
    multi_page: bool = False,
    retry_count: int = 0,
) -> dict:
    """Single call to Gemini API with retry logic."""
    user_text = (
        "Extract every invoice/bill/slip in this document. Read all pages carefully. "
        "Return the `documents` array as instructed."
        if multi_page
        else "Extract the invoice/bill/slip fields. Return the `documents` array (one entry)."
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": media_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
                {"text": user_text},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0,
            "maxOutputTokens": 4096,
        },
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            GEMINI_API_URL,
            params={"key": api_key},
            headers={"content-type": "application/json"},
            json=body,
        )

    if resp.status_code == 429:
        if retry_count < 2:
            await asyncio.sleep(2 ** retry_count)
            return await _call_gemini(
                file_bytes, media_type, api_key,
                multi_page=multi_page, retry_count=retry_count + 1,
            )
        raise ExtractionError(
            "Gemini API quota exceeded — check your plan and billing at "
            "https://ai.google.dev/gemini-api/docs/rate-limits"
        )

    if resp.status_code == 401:
        raise ExtractionError(
            "Invalid GEMINI_API_KEY — check the key in your .env file at "
            "https://aistudio.google.com/apikey"
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


async def _extract_pdf_pages(pages: list[bytes], media_type: str, api_key: str) -> list[dict]:
    """Fallback: extract each PDF page separately and merge results."""
    results = await asyncio.gather(
        *[_call_gemini(page, media_type, api_key) for page in pages],
        return_exceptions=True,
    )
    documents: list[dict] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        for raw_doc in parse_documents_response(result):
            normalized = normalize_document(raw_doc)
            if document_has_data(normalized):
                documents.append(normalized)
    return _dedupe_documents(documents)


async def extract_fields(file_bytes: bytes, content_type: str) -> dict:
    """Extract fields from a document, with multi-page PDF support."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ExtractionError(
            "No GEMINI_API_KEY is set on the server. Add one to your .env file to enable "
            "AI document reading (see README.md) — manual entry still works without it."
        )

    media_type = detect_media_type(file_bytes, content_type)

    if media_type == "application/pdf":
        pages = split_pdf_pages(file_bytes)
        multi_page = len(pages) > 1
        raw = await _call_gemini(file_bytes, media_type, api_key, multi_page=multi_page)
        documents = [
            normalize_document(d)
            for d in parse_documents_response(raw)
        ]
        documents = [d for d in documents if document_has_data(d)]

        if _is_sparse(documents, len(pages)):
            page_docs = await _extract_pdf_pages(pages, media_type, api_key)
            if len(page_docs) > len(documents):
                documents = page_docs
            elif page_docs and not documents:
                documents = page_docs

        return format_api_response(_dedupe_documents(documents))

    file_bytes, media_type = preprocess_image(file_bytes, media_type)
    raw = await _call_gemini(file_bytes, media_type, api_key)
    documents = [
        normalize_document(d)
        for d in parse_documents_response(raw)
    ]
    documents = [d for d in documents if document_has_data(d)]
    return format_api_response(_dedupe_documents(documents))
