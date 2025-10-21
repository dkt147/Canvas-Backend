"""
Users endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Users'])

@router.post("/api/v1/users")
async def create_user(
        user_data: UserCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new user with role-based restrictions"""
    try:
        users_collection = db.users
        orgs_collection = db.organizations

        # ROLE-BASED PERMISSION CHECK
        current_role = current_user["role"]
        target_role = user_data.role

        # Define role hierarchy and permissions
        role_permissions = {
            "super_admin": ["super_admin", "admin_manager", "manager", "canvasser"],
            "admin_manager": ["manager", "canvasser"],
            "manager": ["canvasser"]
        }

        # Check if current user can create the target role
        allowed_roles = role_permissions.get(current_role, [])

        if target_role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{current_role} can only create users with roles: {', '.join(allowed_roles)}"
            )

        # Additional validation: admin_manager can only create users in their own organization
        if current_role == "admin_manager":
            if not user_data.organization_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Admin managers must specify organization_id"
                )
            if user_data.organization_id != current_user["organization_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin managers can only create users in their own organization"
                )

        # Additional validation: manager can only create canvassers in their own organization
        if current_role == "manager":
            if not user_data.organization_id:
                user_data.organization_id = current_user["organization_id"]
            elif user_data.organization_id != current_user["organization_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managers can only create users in their own organization"
                )

            # Auto-assign manager_id when manager creates canvasser
            user_data.manager_id = current_user["username"]

        # Check if username already exists
        if users_collection.find_one({"username": user_data.username}):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists"
            )

        # Check if email already exists
        if users_collection.find_one({"email": user_data.email}):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists"
            )

        # Create user document
        user_doc = {
            "username": user_data.username,
            "password": hash_password(user_data.password),
            "email": user_data.email,
            "role": user_data.role,
            "organization_id": user_data.organization_id,
            "manager_id": user_data.manager_id,
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "phone": user_data.phone,
            "is_active": True,
            "terms_accepted": user_data.terms_accepted,
            "points": user_data.points,
            "created_at": datetime.utcnow(),
            "created_by": current_user["id"]
        }

        result = users_collection.insert_one(user_doc)

        return {
            "message": "User created successfully",
            "user_id": str(result.inserted_id),
            "username": user_data.username,
            "role": user_data.role,
            "created_by_role": current_role
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {str(e)}"
        )

@router.get("/api/v1/users")
async def list_users(
        page: int = 1,
        limit: int = 50,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List users with pagination"""
    try:
        users_collection = db.users

        # Build filter based on user role
        filter_query = {}

        if current_user["role"] == "super_admin":
            # Super admin can see all users
            pass
        elif current_user["role"] == "admin_manager":
            # Admin manager can only see users from their organization
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            # Manager can see canvassers assigned to them
            filter_query = {
                "$or": [
                    {"manager_id": current_user["username"]},
                    {"_id": ObjectId(current_user["id"])}
                ]
            }
        else:
            # Canvasser can only see themselves
            filter_query["_id"] = ObjectId(current_user["id"])

        # Count total documents
        total_count = users_collection.count_documents(filter_query)

        # Calculate pagination
        skip = (page - 1) * limit

        # Get users with pagination
        users = []
        for user in users_collection.find(filter_query).skip(skip).limit(limit).sort("created_at", -1):
            users.append({
                "id": str(user["_id"]),
                "username": user["username"],
                "email": user.get("email"),
                "role": user["role"],
                "organization_id": user.get("organization_id"),
                "organization_name": get_organization_name(user.get("organization_id")),
                "manager_id": user.get("manager_id"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "phone": user.get("phone"),
                "is_active": user.get("is_active", True),
                "terms_accepted": user.get("terms_accepted", True),
                "points": user.get("points", 0),
                "last_activity": user.get("last_activity").isoformat() if user.get("last_activity") else None,
                "created_at": user["created_at"].isoformat()
            })

        return {
            "users": users,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list users: {str(e)}"
        )


@router.get("/api/v1/users/{user_id}")
async def get_user(
        user_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get a specific user by ID"""
    try:
        users_collection = db.users

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Check if current user has access to view this user
        if not check_user_access(current_user, target_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view this user"
            )

        return {
            "id": str(target_user["_id"]),
            "username": target_user["username"],
            "email": target_user.get("email"),
            "role": target_user["role"],
            "organization_id": target_user.get("organization_id"),
            "organization_name": get_organization_name(target_user.get("organization_id")),
            "manager_id": target_user.get("manager_id"),
            "manager_name": get_manager_name(target_user.get("manager_id")),
            "first_name": target_user.get("first_name"),
            "last_name": target_user.get("last_name"),
            "phone": target_user.get("phone"),
            "is_active": target_user.get("is_active", True),
            "terms_accepted": target_user.get("terms_accepted", True),
            "points": target_user.get("points", 0),
            "last_activity": target_user.get("last_activity").isoformat() if target_user.get("last_activity") else None,
            "created_at": target_user["created_at"].isoformat(),
            "created_by": target_user.get("created_by")
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user: {str(e)}"
        )


@router.put("/api/v1/users/{user_id}")
async def update_user(
        user_id: str,
        user_update: UserUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update a user - with role-based restrictions"""
    try:
        users_collection = db.users

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # ROLE-BASED UPDATE PERMISSIONS
        current_role = current_user["role"]
        target_role = target_user["role"]
        can_update = False

        if current_role == "super_admin":
            # Super admin can update anyone
            can_update = True
        elif current_role == "admin_manager":
            # Admin manager can update users in their organization
            # But cannot update other admin_managers or super_admins
            if target_user.get("organization_id") == current_user["organization_id"]:
                if target_role not in ["super_admin", "admin_manager"]:
                    can_update = True
                elif target_role == "admin_manager" and str(target_user["_id"]) == current_user["id"]:
                    # Can update themselves
                    can_update = True
        elif current_role == "manager":
            # Manager can update:
            # 1. Themselves
            # 2. Their assigned canvassers only
            if str(target_user["_id"]) == current_user["id"]:
                can_update = True
            elif target_user.get("manager_id") == current_user["username"] and target_role == "canvasser":
                can_update = True
        elif current_role == "canvasser":
            # Canvasser can only update themselves (limited fields)
            if str(target_user["_id"]) == current_user["id"]:
                can_update = True

        if not can_update:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{current_role} cannot update this user"
            )

        # ROLE CHANGE VALIDATION
        if user_update.role and user_update.role != target_role:
            # Define who can change roles to what
            role_change_permissions = {
                "super_admin": ["super_admin", "admin_manager", "manager", "canvasser"],
                "admin_manager": ["manager", "canvasser"],  # Can't promote to admin_manager or super_admin
                "manager": [],  # Managers cannot change roles at all
                "canvasser": []  # Canvassers cannot change roles at all
            }

            allowed_target_roles = role_change_permissions.get(current_role, [])

            if user_update.role not in allowed_target_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"{current_role} cannot change user role to {user_update.role}"
                )

        # RESTRICT FIELDS CANVASSERS CAN UPDATE
        if current_role == "canvasser":
            # Canvassers can only update their own profile fields
            restricted_fields = ["role", "organization_id", "manager_id", "is_active", "points"]
            for field in restricted_fields:
                if getattr(user_update, field, None) is not None:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Canvassers cannot update the '{field}' field"
                    )

        # Build update document with only provided fields
        update_data = {}

        # Check if trying to update username and if it already exists
        if user_update.username and user_update.username != target_user["username"]:
            existing_user = users_collection.find_one({"username": user_update.username})
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username already exists"
                )
            update_data["username"] = user_update.username

        # Check if trying to update email and if it already exists
        if user_update.email and user_update.email != target_user.get("email"):
            existing_user = users_collection.find_one({"email": user_update.email})
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already exists"
                )
            update_data["email"] = user_update.email

        # Role change (already validated above)
        if user_update.role and user_update.role != target_role:
            update_data["role"] = user_update.role

        # Organization change (only super_admin can do this)
        if user_update.organization_id and user_update.organization_id != target_user.get("organization_id"):
            if current_role != "super_admin":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only super admin can change user organizations"
                )
            update_data["organization_id"] = user_update.organization_id

        # Update other fields if provided
        if user_update.first_name is not None:
            update_data["first_name"] = user_update.first_name
        if user_update.last_name is not None:
            update_data["last_name"] = user_update.last_name
        if user_update.phone is not None:
            update_data["phone"] = user_update.phone
        if user_update.manager_id is not None:
            update_data["manager_id"] = user_update.manager_id
        if hasattr(user_update, 'terms_accepted') and user_update.terms_accepted is not None:
            update_data["terms_accepted"] = user_update.terms_accepted

        # Handle is_active (deactivation/activation) - only admin+
        if user_update.is_active is not None and user_update.is_active != target_user.get("is_active", True):
            if current_role not in ["super_admin", "admin_manager"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to activate/deactivate users"
                )
            update_data["is_active"] = user_update.is_active

        # Handle points (only certain roles can modify points)
        if user_update.points is not None and user_update.points != target_user.get("points", 0):
            if current_role not in ["super_admin", "admin_manager", "manager"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have permission to modify user points"
                )

            old_points = target_user.get("points", 0)
            points_diff = user_update.points - old_points

            update_data["points"] = user_update.points

            # Add points history
            points_history_entry = {
                "action": "update",
                "points": points_diff,
                "old_value": old_points,
                "new_value": user_update.points,
                "reason": "Manual update via API",
                "updated_by": current_user["username"],
                "timestamp": datetime.utcnow()
            }

        # If no updates provided
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )

        # Add updated timestamp
        update_data["updated_at"] = datetime.utcnow()
        update_data["updated_by"] = current_user["id"]

        # Perform the update
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        # Add points history if points were updated
        if user_update.points is not None and user_update.points != target_user.get("points", 0):
            users_collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$push": {"points_history": points_history_entry}}
            )

        # Get updated user
        updated_user = users_collection.find_one({"_id": ObjectId(user_id)})

        return {
            "message": "User updated successfully",
            "user_id": user_id,
            "updated_fields": list(update_data.keys()),
            "updated_by_role": current_role,
            "user": {
                "id": str(updated_user["_id"]),
                "username": updated_user["username"],
                "email": updated_user.get("email"),
                "role": updated_user["role"],
                "organization_id": updated_user.get("organization_id"),
                "first_name": updated_user.get("first_name"),
                "last_name": updated_user.get("last_name"),
                "phone": updated_user.get("phone"),
                "is_active": updated_user.get("is_active", True),
                "points": updated_user.get("points", 0)
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user: {str(e)}"
        )

@router.delete("/api/v1/users/{user_id}")
async def delete_user(
        user_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Delete a user (soft delete by deactivating)"""
    try:
        users_collection = db.users
        leads_collection = db.leads

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Permission check - only super_admin and admin_manager can delete users
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to delete users"
            )

        # Admin managers can only delete users from their organization
        if current_user["role"] == "admin_manager":
            if target_user.get("organization_id") != current_user["organization_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only delete users from your organization"
                )

        # Check if user is already deactivated
        if not target_user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is already deactivated"
            )

        # Prevent self-deletion
        if str(target_user["_id"]) == current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot delete your own account"
            )

        # Check if user has active leads
        active_leads_count = leads_collection.count_documents({
            "created_by": target_user["username"],
            "lead_status": {"$in": ["pending", "approved"]},
            "is_active": True
        })

        # Soft delete - deactivate the user instead of hard delete
        update_data = {
            "is_active": False,
            "deactivated_at": datetime.utcnow(),
            "deactivated_by": current_user["id"],
            "deactivation_reason": "Deleted via API",
            "updated_at": datetime.utcnow()
        }

        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        # If user has active leads, we might want to reassign them or notify
        reassignment_info = {}
        if active_leads_count > 0:
            reassignment_info = {
                "active_leads_count": active_leads_count,
                "note": "User has active leads that may need reassignment"
            }

        return {
            "message": "User deactivated successfully",
            "user_id": user_id,
            "username": target_user["username"],
            "deactivated_at": update_data["deactivated_at"].isoformat(),
            "active_leads_info": reassignment_info
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user: {str(e)}"
        )


@router.post("/api/v1/users/{user_id}/reactivate")
async def reactivate_user(
        user_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Reactivate a deactivated user"""
    try:
        users_collection = db.users

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to reactivate users"
            )

        # Admin managers can only reactivate users from their organization
        if current_user["role"] == "admin_manager":
            if target_user.get("organization_id") != current_user["organization_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only reactivate users from your organization"
                )

        # Check if user is already active
        if target_user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is already active"
            )

        # Reactivate the user
        update_data = {
            "is_active": True,
            "reactivated_at": datetime.utcnow(),
            "reactivated_by": current_user["id"],
            "updated_at": datetime.utcnow()
        }

        # Remove deactivation fields
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": update_data,
                "$unset": {
                    "deactivated_at": "",
                    "deactivated_by": "",
                    "deactivation_reason": ""
                }
            }
        )

        return {
            "message": "User reactivated successfully",
            "user_id": user_id,
            "username": target_user["username"],
            "reactivated_at": update_data["reactivated_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reactivate user: {str(e)}"
        )


@router.post("/api/v1/users/{user_id}/change-password")
async def change_user_password(
        user_id: str,
        password_data: PasswordChange,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Change user password"""
    try:
        users_collection = db.users

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Check permissions - users can change their own password, or admins can change others
        can_change_password = False

        if str(target_user["_id"]) == current_user["id"]:
            # User changing their own password
            can_change_password = True
            # Verify current password
            if not verify_password(password_data.current_password, target_user["password"]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Current password is incorrect"
                )
        elif current_user["role"] in ["super_admin", "admin_manager"]:
            # Admin changing someone else's password
            can_change_password = True
            if current_user["role"] == "admin_manager":
                # Admin manager can only change passwords for users in their organization
                if target_user.get("organization_id") != current_user["organization_id"]:
                    can_change_password = False

        if not can_change_password:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to change this password"
            )

        # Hash new password and update
        hashed_password = hash_password(password_data.new_password)

        update_data = {
            "password": hashed_password,
            "password_changed_at": datetime.utcnow(),
            "password_changed_by": current_user["id"],
            "updated_at": datetime.utcnow()
        }

        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        return {
            "message": "Password changed successfully",
            "user_id": user_id,
            "username": target_user["username"],
            "changed_at": update_data["password_changed_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to change password: {str(e)}"
        )


@router.post("/api/v1/users/{user_id}/reset-password")
async def reset_user_password(
        user_id: str,
        password_reset: PasswordReset,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Reset user password (admin only - no current password required)"""
    try:
        users_collection = db.users

        # Only admins can reset passwords
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to reset passwords"
            )

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Admin managers can only reset passwords for users in their organization
        if current_user["role"] == "admin_manager":
            if target_user.get("organization_id") != current_user["organization_id"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only reset passwords for users in your organization"
                )

        # Hash new password and update
        hashed_password = hash_password(password_reset.new_password)

        update_data = {
            "password": hashed_password,
            "password_reset_at": datetime.utcnow(),
            "password_reset_by": current_user["id"],
            "updated_at": datetime.utcnow()
        }

        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        return {
            "message": "Password reset successfully",
            "user_id": user_id,
            "username": target_user["username"],
            "reset_at": update_data["password_reset_at"].isoformat(),
            "note": "User should change this password on next login"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset password: {str(e)}"
        )


@router.get("/api/v1/users/search")
async def search_users(
        query: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Search users by username, email, or name"""
    try:
        users_collection = db.users

        # Build role-based filter
        role_filter = {}
        if current_user["role"] == "admin_manager":
            role_filter["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            role_filter = {
                "$or": [
                    {"manager_id": current_user["username"]},
                    {"_id": ObjectId(current_user["id"])}
                ]
            }
        elif current_user["role"] == "canvasser":
            role_filter["_id"] = ObjectId(current_user["id"])

        # Create search filter
        search_filter = {
            "$and": [
                role_filter,
                {
                    "$or": [
                        {"username": {"$regex": query, "$options": "i"}},
                        {"email": {"$regex": query, "$options": "i"}},
                        {"first_name": {"$regex": query, "$options": "i"}},
                        {"last_name": {"$regex": query, "$options": "i"}}
                    ]
                }
            ]
        } if role_filter else {
            "$or": [
                {"username": {"$regex": query, "$options": "i"}},
                {"email": {"$regex": query, "$options": "i"}},
                {"first_name": {"$regex": query, "$options": "i"}},
                {"last_name": {"$regex": query, "$options": "i"}}
            ]
        }

        users = []
        for user in users_collection.find(search_filter).limit(20):
            users.append({
                "id": str(user["_id"]),
                "username": user["username"],
                "email": user.get("email"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "role": user["role"],
                "organization_name": get_organization_name(user.get("organization_id")),
                "is_active": user.get("is_active", True)
            })

        return {
            "query": query,
            "results": users,
            "count": len(users)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search users: {str(e)}"
        )


@router.get("/api/v1/users/stats")
async def get_user_stats(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get user statistics"""
    try:
        users_collection = db.users

        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view user statistics"
            )

        # Build filter
        filter_query = {}
        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]

        # Get role distribution
        role_stats = {}
        for role in ["super_admin", "admin_manager", "manager", "canvasser"]:
            role_filter = {**filter_query, "role": role, "is_active": True}
            count = users_collection.count_documents(role_filter)
            role_stats[role] = count

        # Get activity stats
        total_users = users_collection.count_documents({**filter_query, "is_active": True})
        inactive_users = users_collection.count_documents({**filter_query, "is_active": False})

        # Recent activity (last 7 days)
        week_ago = datetime.utcnow() - timedelta(days=7)
        active_this_week = users_collection.count_documents({
            **filter_query,
            "last_activity": {"$gte": week_ago},
            "is_active": True
        })

        return {
            "total_users": total_users,
            "inactive_users": inactive_users,
            "active_this_week": active_this_week,
            "role_distribution": role_stats,
            "activity_rate": round((active_this_week / total_users * 100) if total_users > 0 else 0, 2)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user statistics: {str(e)}"
        )


# ==================== ORGANIZATION MANAGEMENT ====================


