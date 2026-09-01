"""
GET  /api/stocks/search?q=...   — search the fundamentals knowledge base by
                                   ticker/name (autocomplete-style)
GET  /api/stocks/{company_id}   — full fundamentals snapshot for a company

Backed by the Screener.in knowledge base (see backend/screener_kb/schema.sql
and backend/screener_kb/load_screener_data.py) via utils/screener_kb.py.
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from utils.screener_kb import (
    kb_configured,
    search_companies,
    get_company_snapshot_by_id,
    format_snapshot_as_context,
)

router = APIRouter()
log = logging.getLogger("stocks")


@router.get("/search")
async def search(q: str = "", limit: int = 10):
    if not kb_configured():
        return JSONResponse(
            {"error": "Fundamentals knowledge base not configured (SUPABASE_URL/SUPABASE_ANON_KEY missing)"},
            status_code=503,
        )
    if not q.strip():
        return {"results": []}
    results = await search_companies(q.strip(), limit=limit)
    return {"results": results}


@router.get("/{company_id}")
async def snapshot(company_id: int):
    if not kb_configured():
        raise HTTPException(status_code=503, detail="Fundamentals knowledge base not configured")
    snap = await get_company_snapshot_by_id(company_id)
    if not snap or not snap.get("company"):
        raise HTTPException(status_code=404, detail="Company not found")
    return {
        "company": snap["company"],
        "ratios": snap["ratios"],
        "financials": snap["financials"],
        "growth_cagr": snap["growth_cagr"],
        "shareholding": snap["shareholding"],
        "peers": snap["peers"],
        "pros_cons": snap["pros_cons"],
        "context": format_snapshot_as_context(snap),
    }
