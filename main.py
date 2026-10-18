from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")

app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")

class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def read_orders(path: Path) -> list[dict[str, Any]]:
    """Read either a JSON array/object or newline-delimited JSON export."""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("orders", "data", "results", "items"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
            raise ValueError(f"No order array found in {path.name}")
    except json.JSONDecodeError:
        pass
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no} of {path.name}") from exc
    return rows


def load_data():
    orders = read_orders(Path(os.getenv("ORDERS_FILE", BASE_DIR / "export.json")))
    rates_path = Path(os.getenv("RATES_FILE", BASE_DIR / "rates.json"))
    rates_obj = json.loads(rates_path.read_text(encoding="utf-8"))
    rates = {k.upper(): Decimal(str(v)) for k, v in rates_obj["usd_per_unit"].items()}

    # Per assignment instructions, an order ID can have multiple versions;
    # only the row with the latest updated_at is current.
    current: dict[str, dict[str, Any]] = {}
    for row in orders:
        order_id = str(row.get("id", ""))
        if not order_id:
            continue
        previous = current.get(order_id)
        if previous is None or parse_dt(row.get("updated_at")) >= parse_dt(previous.get("updated_at")):
            current[order_id] = row
    return list(current.values()), rates


def parse_dt(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=BUSINESS_TZ)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUSINESS_TZ)
    return dt.astimezone(BUSINESS_TZ)

ORDERS, USD_RATES = load_data()

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
REGIONS = {"north", "south", "east", "west", "central"}
PRODUCTS = sorted({str(o.get("product", "")) for o in ORDERS if o.get("product")}, key=len, reverse=True)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def amount_usd(order: dict[str, Any]) -> Decimal:
    currency = str(order.get("currency", "USD")).upper()
    rate = USD_RATES.get(currency)
    if rate is None:
        raise ValueError(f"No USD conversion rate configured for {currency}")
    return Decimal(str(order.get("amount", 0))) * rate


def parse_filters(question: str) -> dict[str, Any]:
    q = question.lower()
    filters: dict[str, Any] = {}

    for region in REGIONS:
        if re.search(rf"\b{region}\b", q):
            filters["region"] = region.title()
            break

    for product in PRODUCTS:
        if product.lower() in q:
            filters["product"] = product
            break

    customer_match = re.search(r"\b(c\d{4})\b", q, re.I)
    if customer_match:
        filters["customer"] = customer_match.group(1).upper()

    order_match = re.search(r"\b(ord-\d+)\b", q, re.I)
    if order_match:
        filters["id"] = order_match.group(1).upper()

    # Explicit year-month such as 2026-03.
    ym = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", q)
    if ym:
        year, month = int(ym.group(1)), int(ym.group(2))
        filters["start"] = datetime(year, month, 1, tzinfo=BUSINESS_TZ)
        filters["end"] = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=BUSINESS_TZ)
    else:
        month_match = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s+(20\d{2})\b", q)
        if month_match:
            month, year = MONTHS[month_match.group(1)], int(month_match.group(2))
            filters["start"] = datetime(year, month, 1, tzinfo=BUSINESS_TZ)
            filters["end"] = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=BUSINESS_TZ)
        else:
            # A calendar year filter is useful for questions such as "during 2026".
            year_match = re.search(r"\b(20\d{2})\b", q)
            if year_match:
                year = int(year_match.group(1))
                filters["start"] = datetime(year, 1, 1, tzinfo=BUSINESS_TZ)
                filters["end"] = datetime(year + 1, 1, 1, tzinfo=BUSINESS_TZ)

    # Optional named status. Revenue is separately restricted to paid below.
    if "refunded" in q or "refund" in q:
        filters["status"] = "refunded"
    elif re.search(r"\bvoid(?:ed)?\b", q):
        filters["status"] = "void"
    elif re.search(r"\bpaid\b", q):
        filters["status"] = "paid"

    return filters


def filtered_orders(question: str, intent: str) -> list[dict[str, Any]]:
    f = parse_filters(question)
    q = question.lower()
    rows = ORDERS

    # For revenue, sales, and product/customer revenue, only paid orders count.
    if intent == "revenue" or (intent in {"product_revenue", "customer_revenue"}):
        rows = [o for o in rows if str(o.get("status", "")).lower() == "paid"]
    elif f.get("status"):
        rows = [o for o in rows if str(o.get("status", "")).lower() == f["status"]]

    if f.get("region"):
        rows = [o for o in rows if str(o.get("region", "")).lower() == f["region"].lower()]
    if f.get("product"):
        rows = [o for o in rows if str(o.get("product", "")).lower() == f["product"].lower()]
    if f.get("customer"):
        rows = [o for o in rows if str(o.get("customer", "")).upper() == f["customer"]]
    if f.get("id"):
        rows = [o for o in rows if str(o.get("id", "")).upper() == f["id"]]

    if f.get("start"):
        # Use created_at for reporting-period questions; dates are normalized to Asia/Kolkata.
        rows = [o for o in rows if parse_dt(o.get("created_at")) >= f["start"]]
    if f.get("end"):
        rows = [o for o in rows if parse_dt(o.get("created_at")) < f["end"]]
    return rows


def sum_usd(rows: list[dict[str, Any]]) -> Decimal:
    return money(sum((amount_usd(o) for o in rows), Decimal("0")))


def answer_question(question: str) -> Any:
    q = re.sub(r"\s+", " ", question.strip().lower())
    if not q:
        raise ValueError("Question cannot be empty")

    is_refund = any(word in q for word in ("refund", "refunded", "refunds"))
    is_revenue = any(word in q for word in ("revenue", "sales", "turnover", "sold for", "generated"))
    is_customer_revenue = "customer" in q and any(w in q for w in ("spent", "revenue", "sales", "amount", "value", "generated", "highest", "most"))
    is_product_revenue = any(p.lower() in q for p in PRODUCTS) and is_revenue

    if is_refund:
        rows = filtered_orders(question, "refund")
        if any(w in q for w in ("how many", "number of", "count of", "count")) and "amount" not in q and "value" not in q and "total" not in q:
            return len(rows)
        return float(sum_usd(rows))

    if is_customer_revenue:
        rows = filtered_orders(question, "customer_revenue")
        if "customer" not in parse_filters(question):
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for o in rows:
                totals[str(o.get("customer", ""))] += amount_usd(o)
            if not totals:
                return None
            best = max(totals, key=totals.get)
            if any(w in q for w in ("who", "which customer", "top", "highest", "most", "largest")):
                return best
            return float(money(totals[best]))
        return float(sum_usd(rows))

    if is_revenue:
        intent = "product_revenue" if is_product_revenue else "revenue"
        rows = filtered_orders(question, intent)
        # Grouping/ranking questions return the relevant name.
        if any(w in q for w in ("which product", "best-selling product", "top product", "most popular product", "product generated the most", "highest revenue product")):
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for o in rows:
                totals[str(o.get("product", ""))] += amount_usd(o)
            return max(totals, key=totals.get) if totals else None
        if any(w in q for w in ("which region", "top region", "highest region", "most revenue region")):
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for o in rows:
                totals[str(o.get("region", ""))] += amount_usd(o)
            return max(totals, key=totals.get) if totals else None
        return float(sum_usd(rows))

    # Product ranking by quantity must run before the generic quantity branch.
    if any(w in q for w in ("best-selling", "most popular", "sold the most", "most units", "top selling", "top-selling", "most sold", "which product sold")):
        rows = filtered_orders(question, "count")
        totals: dict[str, int] = defaultdict(int)
        for o in rows:
            if str(o.get("status", "")).lower() == "paid":
                totals[str(o.get("product", ""))] += int(o.get("qty", 0))
        return max(totals, key=totals.get) if totals else None

    # Product and quantity questions.
    if any(w in q for w in ("units", "quantity", "how many sold", "number sold", "items sold")):
        rows = filtered_orders(question, "count")
        if "refund" not in q:
            rows = [o for o in rows if str(o.get("status", "")).lower() == "paid"]
        return sum(int(o.get("qty", 0)) for o in rows)

    if any(w in q for w in ("how many orders", "number of orders", "order count", "count orders", "orders did")):
        rows = filtered_orders(question, "count")
        return len(rows)

    if any(w in q for w in ("how many customers", "number of customers", "unique customers", "customer count")):
        rows = filtered_orders(question, "count")
        return len({str(o.get("customer", "")) for o in rows if o.get("customer")})

    # Product count/order count or product total quantity.
    if any(p.lower() in q for p in PRODUCTS):
        rows = filtered_orders(question, "count")
        if "order" in q or "orders" in q:
            return len(rows)
        return sum(int(o.get("qty", 0)) for o in rows if str(o.get("status", "")).lower() == "paid")

    # Customer ranking by order count or revenue.
    if any(w in q for w in ("top customer", "most orders", "most purchases", "most orders placed", "customer ordered the most")):
        rows = filtered_orders(question, "count")
        counts: dict[str, int] = defaultdict(int)
        for o in rows:
            if str(o.get("status", "")).lower() == "paid":
                counts[str(o.get("customer", ""))] += 1
        return max(counts, key=counts.get) if counts else None

    # General totals: default to count of current orders if user explicitly asks for counts.
    rows = filtered_orders(question, "count")
    if "total orders" in q or "all orders" in q:
        return len(rows)

    raise ValueError("I could not determine the requested metric. Try asking for revenue, refunds, order counts, units sold, product rankings, or customer totals.")


@app.get("/")
def health():
    return {"status": "ok", "service": "Acme Appliances order ledger agent", "orders_loaded": len(ORDERS)}


@app.post("/answer")
def answer(req: QuestionRequest):
    try:
        result = answer_question(req.question)
        return {"answer": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        # Do not expose internal data or stack traces in public responses.
        raise HTTPException(status_code=500, detail="Unable to process this question")
