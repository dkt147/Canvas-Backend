"""
News endpoints
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

router = APIRouter(prefix="/api/v1", tags=['News'])

@router.post("/api/v1/news")
async def create_news(
        news_data: NewsCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new news item with optional image upload and organization filtering"""
    try:
        if not check_news_permission(current_user, "create"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to create news"
            )

        # Check pin permission
        if news_data.is_pinned and not check_news_permission(current_user, "pin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin users can pin news"
            )

        # FIXED: Organization assignment logic
        # If organization_specific is True, use creator's organization
        # If False (global news), only super_admin can create
        if news_data.organization_specific:
            # Organization-specific news
            org_id = current_user.get("organization_id")
            if not org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot create organization-specific news without an organization"
                )
        else:
            # Global news - only super_admin can create
            if current_user["role"] != "super_admin":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only super admin can create global news visible to all organizations"
                )
            org_id = None

        # Check news image limits if uploading image
        if news_data.image_base64 and org_id:
            image_limit_check = check_news_image_limits(org_id)
            if not image_limit_check["allowed"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"News image upload not allowed: {image_limit_check['message']}"
                )

        news_collection = db.newss

        # Generate news ID
        news_id = generate_news_id(org_id or "global")

        # Handle image upload
        uploaded_image_id = None
        final_image_url = news_data.image_url

        if news_data.image_base64:
            uploaded_image_id = save_news_image(news_data.image_base64, news_id)
            if uploaded_image_id:
                final_image_url = f"/api/v1/news/images/{uploaded_image_id}"

        # Calculate expiration date
        expiration_date = calculate_expiration_date(news_data.expiration_hours)

        # Create news document
        news_doc = {
            "news_id": news_id,
            "title": news_data.title,
            "content": news_data.content,
            "image_url": final_image_url,
            "uploaded_image_id": uploaded_image_id,
            "priority": news_data.priority.value,
            "expiration_hours": news_data.expiration_hours,
            "expiration_date": expiration_date,
            "is_pinned": news_data.is_pinned,
            "target_roles": news_data.target_roles,
            "organization_id": org_id,  # None for global, org_id for org-specific
            "created_by": current_user["username"],
            "created_at": datetime.utcnow(),
            "is_active": news_data.is_active,
            "read_by": [],
            "pin_order": 0 if news_data.is_pinned else None
        }

        # If pinned, set pin order
        if news_data.is_pinned:
            pinned_count = news_collection.count_documents({
                "is_pinned": True,
                "organization_id": org_id,
                "is_active": True
            })
            news_doc["pin_order"] = pinned_count + 1

        result = news_collection.insert_one(news_doc)

        return {
            "message": "News created successfully",
            "news_id": news_id,
            "database_id": str(result.inserted_id),
            "expires_at": expiration_date.isoformat(),
            "is_pinned": news_data.is_pinned,
            "image_uploaded": bool(uploaded_image_id),
            "image_url": final_image_url,
            "organization_id": org_id,
            "is_global": org_id is None,
            "visible_to": "All organizations" if org_id is None else f"Organization {org_id} only"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create news: {str(e)}"
        )

@router.get("/api/v1/news")
async def list_news(
        page: int = 1,
        limit: int = 20,
        include_expired: bool = False,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List news items for current user"""
    try:
        news_collection = db.newss

        # Build filter for user's accessible news
        filter_query = {"is_active": True}

        # FIXED: Organization filter logic
        if current_user["role"] == "super_admin":
            # Super admin sees ALL news (both global and org-specific)
            pass  # No filter needed
        else:
            # Other roles see:
            # 1. Global news (organization_id = None)
            # 2. Their own organization's news
            org_conditions = [{"organization_id": None}]  # Global news

            if current_user.get("organization_id"):
                org_conditions.append({"organization_id": current_user["organization_id"]})

            filter_query["$or"] = org_conditions

        # Add role filter - news must target user's role or be for all roles
        role_filter = {
            "$or": [
                {"target_roles": []},  # News for all roles
                {"target_roles": current_user["role"]}  # News for user's specific role
            ]
        }

        # Combine organization and role filters
        if "$or" in filter_query:
            # If we already have organization filter, combine with role filter
            filter_query = {
                "$and": [
                    {"$or": filter_query["$or"]},  # Organization conditions
                    role_filter  # Role conditions
                ]
            }
        else:
            # Super admin - just add role filter
            filter_query.update(role_filter)

        # Exclude expired news unless requested
        if not include_expired:
            filter_query["expiration_date"] = {"$gt": datetime.utcnow()}

        # Count total documents
        total_count = news_collection.count_documents(filter_query)

        # Calculate pagination
        skip = (page - 1) * limit

        # Get news items - pinned first, then by creation date
        pipeline = [
            {"$match": filter_query},
            {"$addFields": {
                "sort_priority": {
                    "$cond": {
                        "if": "$is_pinned",
                        "then": 0,  # Pinned items first
                        "else": 1  # Regular items second
                    }
                }
            }},
            {"$sort": {
                "sort_priority": 1,  # Pinned first
                "pin_order": 1,  # Then by pin order
                "created_at": -1  # Then by newest first
            }},
            {"$skip": skip},
            {"$limit": limit}
        ]

        news_items = []
        for news in news_collection.aggregate(pipeline):
            # Check if user has read this news
            is_read = current_user["username"] in news.get("read_by", [])

            # Check if expired
            is_expired = is_news_expired(news["expiration_date"])

            # Get creator info
            users_collection = db.users
            creator = users_collection.find_one({"username": news["created_by"]})
            creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
            news["created_by"]
            if not creator_name:
                creator_name = news["created_by"]

            # Calculate time remaining
            time_remaining = news["expiration_date"] - datetime.utcnow()
            hours_remaining = int(time_remaining.total_seconds() / 3600) if time_remaining.total_seconds() > 0 else 0

            news_items.append({
                "id": str(news["_id"]),
                "news_id": news["news_id"],
                "title": news["title"],
                "content": news["content"],
                "image_url": news.get("image_url"),
                "priority": news["priority"],
                "expiration_hours": news["expiration_hours"],
                "expiration_date": news["expiration_date"].isoformat(),
                "hours_remaining": hours_remaining,
                "is_expired": is_expired,
                "is_pinned": news.get("is_pinned", False),
                "pin_order": news.get("pin_order"),
                "target_roles": news["target_roles"],
                "organization_id": news.get("organization_id"),
                "created_by": news["created_by"],
                "created_by_name": creator_name,
                "created_at": news["created_at"].isoformat(),
                "is_read": is_read,
                "is_global": news.get("organization_id") is None
            })

        return {
            "news": news_items,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "unread_count": sum(1 for item in news_items if not item["is_read"]),
            "pinned_count": sum(1 for item in news_items if item["is_pinned"]),
            "expired_count": sum(1 for item in news_items if item["is_expired"])
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list news: {str(e)}"
        )

@router.post("/api/v1/news/{news_id}/mark-read")
async def mark_news_read(
        news_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Mark a news item as read"""
    try:
        news_collection = db.newss

        # Find news item
        news = news_collection.find_one({"news_id": news_id})
        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        # Add user to read_by list if not already there
        if current_user["username"] not in news.get("read_by", []):
            news_collection.update_one(
                {"news_id": news_id},
                {"$addToSet": {"read_by": current_user["username"]}}
            )

        return {"message": "News marked as read"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark news as read: {str(e)}"
        )


@router.get("/api/v1/news/unread-count")
async def get_unread_news_count(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get count of unread news for current user"""
    try:
        news_collection = db.newss

        # Build filter for user's accessible news
        filter_query = {
            "is_active": True,
            "expiration_date": {"$gt": datetime.utcnow()},  # Not expired
            "read_by": {"$ne": current_user["username"]},  # Not read by current user
            "$and": [
                {"$or": [
                    {"organization_id": None},
                    {"organization_id": current_user.get("organization_id")}
                ]},
                {"$or": [
                    {"target_roles": []},
                    {"target_roles": current_user["role"]}
                ]}
            ]
        }

        unread_count = news_collection.count_documents(filter_query)

        # Count urgent unread
        urgent_filter = {**filter_query, "priority": "urgent"}
        urgent_unread = news_collection.count_documents(urgent_filter)

        # Count pinned unread
        pinned_filter = {**filter_query, "is_pinned": True}
        pinned_unread = news_collection.count_documents(pinned_filter)

        return {
            "unread_count": unread_count,
            "urgent_unread": urgent_unread,
            "pinned_unread": pinned_unread
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get unread count: {str(e)}"
        )


@router.post("/api/v1/news/{news_id}/pin")
async def toggle_pin_news(
        news_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Pin or unpin a news item (Admin only)"""
    try:
        if not check_news_permission(current_user, "pin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin users can pin/unpin news"
            )

        news_collection = db.newss

        # Find news item
        news = news_collection.find_one({"news_id": news_id})
        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        current_pin_status = news.get("is_pinned", False)
        new_pin_status = not current_pin_status

        update_data = {
            "is_pinned": new_pin_status,
            "updated_at": datetime.utcnow(),
            "updated_by": current_user["username"]
        }

        if new_pin_status:
            # Setting pin - assign pin order
            pinned_count = news_collection.count_documents({
                "is_pinned": True,
                "organization_id": news.get("organization_id"),
                "is_active": True
            })
            update_data["pin_order"] = pinned_count + 1
        else:
            # Removing pin
            update_data["pin_order"] = None

        news_collection.update_one(
            {"news_id": news_id},
            {"$set": update_data}
        )

        action = "pinned" if new_pin_status else "unpinned"
        return {
            "message": f"News {action} successfully",
            "news_id": news_id,
            "is_pinned": new_pin_status
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to toggle pin: {str(e)}"
        )


@router.put("/api/v1/news/{news_id}")
async def update_news(
        news_id: str,
        news_update: NewsUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update a news item"""
    try:
        if not check_news_permission(current_user, "update"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to update news"
            )

        news_collection = db.newss

        # Find news item
        news = news_collection.find_one({"news_id": news_id})
        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        # Build update document
        update_data = {}
        if news_update.title is not None:
            update_data["title"] = news_update.title
        if news_update.content is not None:
            update_data["content"] = news_update.content
        if news_update.image_url is not None:
            update_data["image_url"] = news_update.image_url
        if news_update.priority is not None:
            update_data["priority"] = news_update.priority.value
        if news_update.expiration_hours is not None:
            update_data["expiration_hours"] = news_update.expiration_hours
            update_data["expiration_date"] = calculate_expiration_date(news_update.expiration_hours)
        if news_update.target_roles is not None:
            update_data["target_roles"] = news_update.target_roles
        if news_update.is_active is not None:
            update_data["is_active"] = news_update.is_active

        # Handle pinning (admin only)
        if news_update.is_pinned is not None:
            if not check_news_permission(current_user, "pin"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only admin users can pin/unpin news"
                )
            update_data["is_pinned"] = news_update.is_pinned

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )

        update_data["updated_at"] = datetime.utcnow()
        update_data["updated_by"] = current_user["username"]

        news_collection.update_one(
            {"news_id": news_id},
            {"$set": update_data}
        )

        return {
            "message": "News updated successfully",
            "news_id": news_id,
            "updated_fields": list(update_data.keys())
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update news: {str(e)}"
        )


@router.delete("/api/v1/news/{news_id}")
async def delete_news(
        news_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Delete a news item (soft delete)"""
    try:
        if not check_news_permission(current_user, "delete"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to delete news"
            )

        news_collection = db.newss

        # Find news item
        news = news_collection.find_one({"news_id": news_id})
        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        # Soft delete
        update_data = {
            "is_active": False,
            "deleted_at": datetime.utcnow(),
            "deleted_by": current_user["username"]
        }

        news_collection.update_one(
            {"news_id": news_id},
            {"$set": update_data}
        )

        return {
            "message": "News deleted successfully",
            "news_id": news_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete news: {str(e)}"
        )


@router.delete("/api/v1/news/cleanup-expired")
async def cleanup_expired_news(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Cleanup expired news items"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to cleanup news"
            )

        news_collection = db.newss

        # Find expired news
        expired_filter = {
            "expiration_date": {"$lt": datetime.utcnow()},
            "is_active": True
        }

        # Add organization filter for admin_manager
        if current_user["role"] == "admin_manager":
            expired_filter["organization_id"] = current_user["organization_id"]

        expired_count = news_collection.count_documents(expired_filter)

        # Soft delete expired news
        news_collection.update_many(
            expired_filter,
            {"$set": {
                "is_active": False,
                "expired_at": datetime.utcnow(),
                "expired_by": current_user["username"]
            }}
        )

        return {
            "message": "Expired news cleaned up successfully",
            "expired_count": expired_count
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup expired news: {str(e)}"
        )


# Add these new endpoints to main.py


