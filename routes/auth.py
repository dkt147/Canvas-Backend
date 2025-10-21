"""
Authentication endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Authentication'])

@router.post("/api/v1/auth/login", response_model=Token)
async def login(user_data: UserLogin):
    """Login endpoint - returns JWT token"""
    try:
        users_collection = db.users
        user = users_collection.find_one({"username": user_data.username})

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )

        if not verify_password(user_data.password, user["password"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )

        if not user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is deactivated"
            )

        # Update last activity
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"last_activity": datetime.utcnow()}}
        )

        token_data = {
            "sub": str(user["_id"]),
            "username": user["username"],
            "role": user["role"],
            "organization_id": user.get("organization_id")
        }

        access_token = create_access_token(token_data)

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user_info": {
                "username": user["username"],
                "role": user["role"],
                "organization_id": user.get("organization_id"),
                "email": user.get("email"),
                "points": user.get("points", 0)
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Login failed: {str(e)}"
        )


@router.get("/api/v1/auth/me")
async def get_current_user(current_user: dict = Depends(get_current_user_from_token)):
    """Get current user info"""
    users_collection = db.users
    user = users_collection.find_one({"_id": ObjectId(current_user["id"])})

    return {
        "id": current_user["id"],
        "username": user["username"],
        "email": user.get("email"),
        "role": user["role"],
        "organization_id": user.get("organization_id"),
        "organization_name": get_organization_name(user.get("organization_id")),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "phone": user.get("phone"),
        "points": user.get("points", 0),
        "is_active": user.get("is_active", True),
        "terms_accepted": user.get("terms_accepted", False)
    }


# ==================== USER MANAGEMENT ====================

