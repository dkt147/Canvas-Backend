"""
Organizations endpoints
"""
from fastapi import APIRouter, HTTPException, status, Depends, Query, File, UploadFile
from fastapi.responses import StreamingResponse
from typing import Optional, List
from datetime import datetime, timedelta
from bson import ObjectId
import io
import csv

from app.core.security import get_current_user_from_token
from app.core.database import db
from app.models.schemas import *
from app.models.enums import *

router = APIRouter(prefix="/api/v1", tags=['Organizations'])

@router.post("/api/v1/organizations")
async def create_organization(
        org_data: OrganizationCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new organization with plan limits"""
    try:
        if current_user.get("role") != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can create organizations"
            )

        orgs_collection = db.organizations

        # Check if organization with same email already exists
        existing_org = orgs_collection.find_one({"email": org_data.email})
        if existing_org:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Organization with this email already exists"
            )

        # Generate unique org_id
        org_id = f"org_{str(uuid.uuid4())[:8]}"

        # Get plan limits
        plan_limits = get_organization_limits(org_data.plan.value)

        # Create organization document with plan
        org_doc = {
            "org_id": org_id,
            "name": org_data.name,
            "email": org_data.email,
            "max_users": org_data.max_users,
            "industry": org_data.industry,
            "address": org_data.address,
            "phone": org_data.phone,
            "plan": org_data.plan.value,  # Add this line
            "plan_limits": plan_limits,     # Add this line
            "is_active": True,
            "created_at": datetime.utcnow(),
            "created_by": current_user["id"]
        }

        result = orgs_collection.insert_one(org_doc)

        return {
            "message": "Organization created successfully",
            "organization_id": org_id,
            "database_id": str(result.inserted_id),
            "plan": org_data.plan.value,
            "plan_limits": plan_limits
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create organization: {str(e)}"
        )

@router.get("/api/v1/organizations")
async def list_organizations(
        current_user: dict = Depends(get_current_user_from_token)
):
    """List all organizations"""
    try:
        if current_user.get("role") != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can list all organizations"
            )

        orgs_collection = db.organizations
        users_collection = db.users

        organizations = []

        for org in orgs_collection.find({}):
            user_count = users_collection.count_documents({
                "organization_id": org["org_id"],
                "is_active": True
            })

            organizations.append({
                "id": str(org["_id"]),
                "org_id": org["org_id"],
                "name": org["name"],
                "email": org["email"],
                "max_users": org["max_users"],
                "current_users": user_count,
                "industry": org.get("industry", "Construction"),
                "address": org.get("address"),
                "phone": org.get("phone"),
                "is_active": org.get("is_active", True),
                "created_at": org["created_at"].isoformat()
            })

        return {
            "organizations": organizations,
            "total_count": len(organizations)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list organizations: {str(e)}"
        )


# ==================== POINTS MANAGEMENT ====================


