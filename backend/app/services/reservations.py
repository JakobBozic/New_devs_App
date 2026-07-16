import asyncio
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings

# Money is always rounded to whole cents. NUMERIC(10,3) lets the database store
# sub-cent amounts (e.g. 333.333), so we must round the *aggregate* exactly once,
# using Decimal arithmetic - never float - to avoid the "off by a few cents" drift.
_CENTS = Decimal("0.01")

_engine: Optional[Engine] = None


def _get_engine() -> Engine:
    """Lazily build a synchronous engine against the configured database.

    Revenue used to be routed through an async pool that read non-existent
    ``settings.supabase_db_*`` values (and required a driver that isn't
    installed), so it always failed and silently returned hard-coded mock
    totals. We connect to the database that is actually configured via
    ``settings.database_url`` (the local Postgres in challenge mode).
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine


def _to_cents(value: Any) -> Decimal:
    """Round any monetary value to cents with half-up rounding (never via float)."""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _month_window(month: int, year: int, tz_name: str):
    """Return the [start, end) instants of a calendar month in the given timezone.

    The boundaries are timezone-aware, so a booking is attributed to the month it
    falls in *for that property's local time* - not in UTC. e.g. a check-in at
    2024-02-29 23:30Z for a Europe/Paris property is 2024-03-01 00:30 local, i.e.
    March, and must be counted in March.
    """
    tz = ZoneInfo(tz_name)
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return start, end


def _query_total(property_id: str, tenant_id: str) -> Dict[str, Any]:
    engine = _get_engine()
    query = text(
        """
        SELECT
            COALESCE(SUM(total_amount), 0) AS total_revenue,
            COUNT(*)                       AS reservation_count,
            COALESCE(MIN(currency), 'USD') AS currency
        FROM reservations
        WHERE property_id = :property_id
          AND tenant_id   = :tenant_id
        """
    )
    with engine.connect() as conn:
        row = conn.execute(
            query, {"property_id": property_id, "tenant_id": tenant_id}
        ).one()

    total = _to_cents(row.total_revenue)
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": str(total),
        "currency": row.currency,
        "count": int(row.reservation_count),
    }


def _query_monthly(property_id: str, tenant_id: str, month: int, year: int) -> Decimal:
    engine = _get_engine()
    with engine.connect() as conn:
        tz_row = conn.execute(
            text(
                "SELECT timezone FROM properties "
                "WHERE id = :property_id AND tenant_id = :tenant_id"
            ),
            {"property_id": property_id, "tenant_id": tenant_id},
        ).first()
        tz_name = tz_row.timezone if tz_row and tz_row.timezone else "UTC"

        start, end = _month_window(month, year, tz_name)
        row = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(total_amount), 0) AS total
                FROM reservations
                WHERE property_id = :property_id
                  AND tenant_id   = :tenant_id
                  AND check_in_date >= :start
                  AND check_in_date <  :end
                """
            ),
            {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "start": start,
                "end": end,
            },
        ).one()

    return _to_cents(row.total)


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, month: int, year: int
) -> Decimal:
    """Revenue for a property in a given calendar month, in the property's timezone.

    Scoped by both ``property_id`` and ``tenant_id`` and bucketed using the
    property's local-time month boundaries, so bookings that straddle a
    month/timezone boundary land in the month the client actually reports them.
    """
    return await asyncio.to_thread(_query_monthly, property_id, tenant_id, month, year)


async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """Aggregate a property's revenue directly from the reservations table.

    Always scoped by BOTH ``property_id`` and ``tenant_id``, summed with exact
    Decimal arithmetic and rounded to cents once. It queries the real database
    and raises on failure instead of silently returning fabricated mock totals,
    so the dashboard can never display numbers that don't reconcile with the
    underlying reservations.
    """
    return await asyncio.to_thread(_query_total, property_id, tenant_id)


def _query_properties(tenant_id: str) -> List[Dict[str, Any]]:
    engine = _get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, name, timezone FROM properties "
                "WHERE tenant_id = :tenant_id ORDER BY id"
            ),
            {"tenant_id": tenant_id},
        ).all()
    return [{"id": r.id, "name": r.name, "timezone": r.timezone} for r in rows]


async def list_properties(tenant_id: str) -> List[Dict[str, Any]]:
    """Return the properties owned by a single tenant.

    Scoped by ``tenant_id`` so each client only ever sees their own properties -
    the dashboard must never render a hard-coded, tenant-agnostic list.
    """
    return await asyncio.to_thread(_query_properties, tenant_id)
