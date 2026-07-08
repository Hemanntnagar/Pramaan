from app.ai_extract import (
    detect_media_type,
    document_has_data,
    normalize_document,
    parse_documents_response,
    _dedupe_documents,
)


def test_detect_media_type_from_magic_bytes():
    assert detect_media_type(b"%PDF-1.4", "application/octet-stream") == "application/pdf"
    assert detect_media_type(b"\xff\xd8\xff", None) == "image/jpeg"
    assert detect_media_type(b"\x89PNG\r\n\x1a\n", "") == "image/png"


def test_normalize_indian_date_formats():
    doc = normalize_document({"invoice_no": "INV-1", "date": "04/04/2026", "quantity_kg": 4200})
    assert doc["date"] == "2026-04-04"


def test_normalize_tonnes_to_kg():
    doc = normalize_document({"quantity_kg": 4.2, "invoice_no": "X", "weight_mt": 4.2})
    assert doc["quantity_kg"] == 4200.0


def test_normalize_computes_rate_from_total():
    doc = normalize_document({"quantity_kg": 1000, "total_amount": "27,000"})
    assert doc["rate_per_kg"] == 27.0


def test_normalize_field_aliases():
    doc = normalize_document({
        "bill_no": "EWBN123",
        "invoice_date": "2026-04-04",
        "net_weight": "3,100",
        "unit_price": "25",
        "consignee": "Shree Polymers Pvt. Ltd.",
    })
    assert doc["invoice_no"] == "EWBN123"
    assert doc["quantity_kg"] == 3100.0
    assert doc["rate_per_kg"] == 25.0
    assert doc["facility_name"] == "Shree Polymers Pvt. Ltd."


def test_parse_documents_response_array_and_legacy():
    wrapped = parse_documents_response({"documents": [{"invoice_no": "A"}]})
    assert len(wrapped) == 1
    legacy = parse_documents_response({"invoice_no": "B"})
    assert legacy[0]["invoice_no"] == "B"


def test_dedupe_documents():
    docs = [
        {"invoice_no": "INV-1", "date": "2026-04-01", "quantity_kg": 100},
        {"invoice_no": "INV-1", "date": "2026-04-01", "quantity_kg": 100},
        {"invoice_no": "INV-2", "date": "2026-04-02", "quantity_kg": 200},
    ]
    assert len(_dedupe_documents(docs)) == 2


def test_document_has_data():
    assert document_has_data({"invoice_no": "X"}) is True
    assert document_has_data({"facility_name": "Only name"}) is False
