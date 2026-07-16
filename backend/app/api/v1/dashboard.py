from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, List
from decimal import Decimal
from app.services.cache import get_revenue_summary
from app.services.reservations import list_properties
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()


@router.get("/dashboard/properties")
async def get_dashboard_properties(
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """Return only the authenticated tenant's properties for the dashboard.

    The tenant is taken from the authenticated user, never from the client, so
    each client sees exactly their own properties.
    """
    tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"
    return await list_properties(tenant_id)

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"

    revenue_data = await get_revenue_summary(property_id, tenant_id)

    # revenue_data['total'] is already rounded to whole cents as an exact Decimal
    # string. Parse it back through Decimal (not float(str)) so the value stays
    # penny-exact, then hand the JSON layer a clean 2-decimal number.
    total_revenue = float(Decimal(revenue_data['total']))

    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": total_revenue,
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }
