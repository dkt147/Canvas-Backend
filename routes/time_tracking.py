"""
Time Tracking endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Time Tracking'])

@router.post("/api/v1/time-tracking/clock-in")
async def clock_in(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Clock in user and start location tracking"""
    try:
        # Only canvassers and managers can clock in
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only canvassers and managers can use time tracking"
            )

        time_tracking_collection = db.time_tracking

        # Check if user is already clocked in
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is already clocked in"
            )

        # Create new time tracking session
        session_doc = {
            "user_id": current_user["id"],
            "username": current_user["username"],
            "organization_id": current_user["organization_id"],
            "clock_in_time": datetime.utcnow(),
            "clock_out_time": None,
            "total_hours": None,
            "location_points": [],
            "is_active": True,
            "created_at": datetime.utcnow()
        }

        result = time_tracking_collection.insert_one(session_doc)

        return {
            "message": "Clocked in successfully",
            "session_id": str(result.inserted_id),
            "clock_in_time": session_doc["clock_in_time"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clock in: {str(e)}"
        )


@router.post("/api/v1/time-tracking/clock-out-updated")
async def clock_out_with_break_handling(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Clock out user with proper break handling"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only canvassers and managers can use time tracking"
            )

        time_tracking_collection = db.time_tracking
        live_tracking_collection = db.live_tracking  # ADD THIS LINE

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active clock-in session found"
            )

        clock_out_time = datetime.utcnow()
        clock_in_time = active_session["clock_in_time"]

        # Handle any active break
        breaks = active_session.get("breaks", [])
        active_break = get_active_break(active_session)

        if active_break:
            # Auto-end the active break
            for i, break_item in enumerate(breaks):
                if break_item.get("break_id") == active_break["break_id"]:
                    duration_minutes = calculate_break_duration({
                        "start_time": active_break["start_time"],
                        "end_time": clock_out_time
                    })
                    breaks[i].update({
                        "end_time": clock_out_time,
                        "duration_minutes": duration_minutes,
                        "status": BreakStatus.COMPLETED.value,
                        "notes": "Auto-ended on clock out",
                        "completed_by": current_user["username"]
                    })
                    break

        # Calculate total break time
        total_break_minutes = sum(
            b.get("duration_minutes", 0) for b in breaks
            if b.get("status") == "completed"
        )

        # Calculate total session time and work time
        total_session_time = clock_out_time - clock_in_time
        total_session_hours = total_session_time.total_seconds() / 3600
        work_hours = max(0, total_session_hours - (total_break_minutes / 60))

        # Update session
        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {"$set": {
                "clock_out_time": clock_out_time,
                "total_hours": round(total_session_hours, 2),
                "work_hours": round(work_hours, 2),
                "total_break_minutes": total_break_minutes,
                "breaks": breaks,
                "is_active": False,
                "on_break": False,
                "updated_at": clock_out_time
            }}
        )

        # ADD THIS SECTION: Clean up live tracking data
        live_tracking_collection.update_one(
            {"user_id": current_user["id"], "is_active": True},
            {"$set": {
                "is_active": False,
                "clocked_out_at": clock_out_time,
                "final_location": None  # Optional: clear location data
            }}
        )

        return {
            "message": "Clocked out successfully",
            "session_id": str(active_session["_id"]),
            "clock_in_time": clock_in_time.isoformat(),
            "clock_out_time": clock_out_time.isoformat(),
            "total_session_hours": round(total_session_hours, 2),
            "work_hours": round(work_hours, 2),
            "break_minutes": total_break_minutes,
            "active_break_auto_ended": bool(active_break)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clock out: {str(e)}"
        )
@router.post("/api/v1/time-tracking/clock-out")
async def clock_out(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Clock out user and calculate total hours"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only canvassers and managers can use time tracking"
            )

        time_tracking_collection = db.time_tracking
        live_tracking_collection = db.live_tracking  # ADD THIS

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active clock-in session found"
            )

        clock_out_time = datetime.utcnow()
        clock_in_time = active_session["clock_in_time"]

        # Calculate total hours
        time_diff = clock_out_time - clock_in_time
        total_hours = round(time_diff.total_seconds() / 3600, 2)

        # Update session
        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {"$set": {
                "clock_out_time": clock_out_time,
                "total_hours": total_hours,
                "is_active": False,
                "updated_at": clock_out_time
            }}
        )

        # ADD THIS: Clean up live tracking data
        live_tracking_collection.update_one(
            {"user_id": current_user["id"], "is_active": True},
            {"$set": {
                "is_active": False,
                "clocked_out_at": clock_out_time
            }}
        )

        return {
            "message": "Clocked out successfully",
            "session_id": str(active_session["_id"]),
            "clock_in_time": clock_in_time.isoformat(),
            "clock_out_time": clock_out_time.isoformat(),
            "total_hours": total_hours
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clock out: {str(e)}"
        )
# Step 5: Update the time tracking status endpoint
@router.get("/api/v1/time-tracking/status-with-breaks")
async def get_clock_status_with_breaks(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current clock in/out status including break information"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            return {"is_clocked_in": False}

        time_tracking_collection = db.time_tracking

        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if active_session:
            # Calculate current elapsed time
            elapsed_time = datetime.utcnow() - active_session["clock_in_time"]
            elapsed_hours = elapsed_time.total_seconds() / 3600

            # Get break information
            breaks = active_session.get("breaks", [])
            active_break = get_active_break(active_session)

            total_break_minutes = sum(
                calculate_break_duration(b) for b in breaks
                if b.get("status") in ["completed", "active"]
            )

            work_hours = max(0, elapsed_hours - (total_break_minutes / 60))

            # Get daily limits
            limits = validate_daily_limits(current_user["id"])

            response = {
                "is_clocked_in": True,
                "session_id": str(active_session["_id"]),
                "clock_in_time": active_session["clock_in_time"].isoformat(),
                "elapsed_hours": round(elapsed_hours, 2),
                "work_hours": round(work_hours, 2),
                "break_minutes": round(total_break_minutes, 2),
                "daily_limits": limits
            }

            if active_break:
                break_duration = calculate_break_duration(active_break)
                response.update({
                    "is_on_break": True,
                    "current_break": {
                        "break_id": active_break["break_id"],
                        "type": active_break["break_type"],
                        "start_time": active_break["start_time"].isoformat(),
                        "duration_minutes": break_duration,
                        "is_overtime": break_duration > 45
                    }
                })
            else:
                response["is_on_break"] = False

            return response
        else:
            return {"is_clocked_in": False}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get clock status: {str(e)}"
        )
@router.get("/api/v1/time-tracking/status")
async def get_clock_status(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current clock in/out status"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            return {"is_clocked_in": False}

        time_tracking_collection = db.time_tracking

        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if active_session:
            # Calculate current elapsed time
            elapsed_time = datetime.utcnow() - active_session["clock_in_time"]
            elapsed_hours = round(elapsed_time.total_seconds() / 3600, 2)

            return {
                "is_clocked_in": True,
                "session_id": str(active_session["_id"]),
                "clock_in_time": active_session["clock_in_time"].isoformat(),
                "elapsed_hours": elapsed_hours
            }
        else:
            return {"is_clocked_in": False}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get clock status: {str(e)}"
        )


@router.post("/api/v1/time-tracking/update-location")
async def update_location(
        latitude: float,
        longitude: float,
        accuracy: Optional[float] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update location during active time tracking session"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Location tracking only available for canvassers and managers"
            )

        time_tracking_collection = db.time_tracking

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active session found. Please clock in first."
            )

        # Add location point
        location_point = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
            "timestamp": datetime.utcnow()
        }

        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {"$push": {"location_points": location_point}}
        )

        return {
            "message": "Location updated successfully",
            "timestamp": location_point["timestamp"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update location: {str(e)}"
        )


@router.get("/api/v1/time-tracking/active-users")
async def get_active_users(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get all currently clocked-in users (for managers/admins)"""
    try:
        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view active users"
            )

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Build filter based on role
        filter_query = {
            "clock_out_time": None,
            "is_active": True
        }

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            # Get assigned canvassers
            assigned_canvassers = []
            for user in users_collection.find({"manager_id": current_user["username"]}):
                assigned_canvassers.append(user["_id"])

            filter_query["user_id"] = {"$in": [str(uid) for uid in assigned_canvassers]}

        active_users = []
        for session in time_tracking_collection.find(filter_query):
            # Get user details
            user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
            if not user:
                continue

            # Calculate elapsed time
            elapsed_time = datetime.utcnow() - session["clock_in_time"]
            elapsed_hours = round(elapsed_time.total_seconds() / 3600, 2)

            # Get latest location
            latest_location = None
            if session.get("location_points"):
                latest_location = session["location_points"][-1]

            active_users.append({
                "session_id": str(session["_id"]),
                "user_id": session["user_id"],
                "username": session["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "clock_in_time": session["clock_in_time"].isoformat(),
                "elapsed_hours": elapsed_hours,
                "latest_location": latest_location,
                "location_count": len(session.get("location_points", []))
            })

        return {
            "active_users": active_users,
            "total_count": len(active_users)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get active users: {str(e)}"
        )


@router.get("/api/v1/time-tracking/history")
async def get_time_history(
        user_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get time tracking history"""
    try:
        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Build filter
        filter_query = {}

        # Role-based access control
        if current_user["role"] == "canvasser":
            filter_query["user_id"] = current_user["id"]
        elif current_user["role"] == "manager":
            if user_id and user_id != current_user["id"]:
                # Check if user is assigned to this manager
                target_user = users_collection.find_one({"_id": ObjectId(user_id)})
                if not target_user or target_user.get("manager_id") != current_user["username"]:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="You can only view time tracking for your assigned canvassers"
                    )
                filter_query["user_id"] = user_id
            else:
                # Show manager's own history and their canvassers
                assigned_user_ids = [current_user["id"]]
                for user in users_collection.find({"manager_id": current_user["username"]}):
                    assigned_user_ids.append(str(user["_id"]))
                filter_query["user_id"] = {"$in": assigned_user_ids}
        elif current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
            if user_id:
                filter_query["user_id"] = user_id
        # super_admin can see all

        # Date filtering
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                filter_query["clock_in_time"] = {"$gte": start_dt}
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid start_date format. Use YYYY-MM-DD"
                )

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00')) + timedelta(days=1)
                if "clock_in_time" in filter_query:
                    filter_query["clock_in_time"]["$lt"] = end_dt
                else:
                    filter_query["clock_in_time"] = {"$lt": end_dt}
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid end_date format. Use YYYY-MM-DD"
                )

        # Count total
        total_count = time_tracking_collection.count_documents(filter_query)

        # Pagination
        skip = (page - 1) * limit

        sessions = []
        for session in time_tracking_collection.find(filter_query).skip(skip).limit(limit).sort("clock_in_time", -1):
            # Get user info
            user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
            user_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() if user else session[
                "username"]

            sessions.append({
                "session_id": str(session["_id"]),
                "user_id": session["user_id"],
                "username": session["username"],
                "user_name": user_name,
                "clock_in_time": session["clock_in_time"].isoformat(),
                "clock_out_time": session["clock_out_time"].isoformat() if session.get("clock_out_time") else None,
                "total_hours": session.get("total_hours"),
                "is_active": session.get("is_active", False),
                "location_points_count": len(session.get("location_points", []))
            })

        return {
            "sessions": sessions,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get time history: {str(e)}"
        )


@router.get("/api/v1/time-tracking/reports/summary")
async def get_time_summary(
        user_id: Optional[str] = None,
        period: str = "week",  # week, month, year
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get time tracking summary report"""
    try:
        time_tracking_collection = db.time_tracking

        # Calculate date range
        now = datetime.utcnow()
        if period == "week":
            start_date = now - timedelta(days=7)
        elif period == "month":
            start_date = now - timedelta(days=30)
        elif period == "year":
            start_date = now - timedelta(days=365)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid period. Use: week, month, or year"
            )

        # Build filter with permissions
        filter_query = {
            "clock_in_time": {"$gte": start_date},
            "clock_out_time": {"$ne": None}
        }

        # Apply role-based filtering (same logic as history)
        if current_user["role"] == "canvasser":
            filter_query["user_id"] = current_user["id"]
        elif current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]

        if user_id:
            filter_query["user_id"] = user_id

        # Aggregate data
        pipeline = [
            {"$match": filter_query},
            {"$group": {
                "_id": None,
                "total_hours": {"$sum": "$total_hours"},
                "total_sessions": {"$sum": 1},
                "avg_hours_per_session": {"$avg": "$total_hours"}
            }}
        ]

        result = list(time_tracking_collection.aggregate(pipeline))

        if result:
            summary = result[0]
            return {
                "period": period,
                "start_date": start_date.isoformat(),
                "end_date": now.isoformat(),
                "total_hours": round(summary["total_hours"], 2),
                "total_sessions": summary["total_sessions"],
                "avg_hours_per_session": round(summary["avg_hours_per_session"], 2) if summary[
                    "avg_hours_per_session"] else 0
            }
        else:
            return {
                "period": period,
                "start_date": start_date.isoformat(),
                "end_date": now.isoformat(),
                "total_hours": 0,
                "total_sessions": 0,
                "avg_hours_per_session": 0
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get time summary: {str(e)}"
        )


# ==================== ORGANIZATION DELETE METHODS ====================

@router.delete("/api/v1/organizations/{org_id}")
async def delete_organization(
        org_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Delete an organization (soft delete by deactivating)"""
    try:
        # Only super admin can delete organizations
        if current_user.get("role") != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can delete organizations"
            )

        orgs_collection = db.organizations
        users_collection = db.users
        leads_collection = db.leads

        # Find organization
        if len(org_id) == 24:
            org = orgs_collection.find_one({"_id": ObjectId(org_id)})
            org_filter = {"_id": ObjectId(org_id)}
        else:
            org = orgs_collection.find_one({"org_id": org_id})
            org_filter = {"org_id": org_id}

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Check if already deactivated
        if not org.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Organization is already deactivated"
            )

        # Count active users and leads
        org_identifier = org.get("org_id")
        active_users = users_collection.count_documents({
            "organization_id": org_identifier,
            "is_active": True
        })

        active_leads = leads_collection.count_documents({
            "organization_id": org_identifier,
            "is_active": True
        })

        # Soft delete - deactivate the organization
        update_data = {
            "is_active": False,
            "deactivated_at": datetime.utcnow(),
            "deactivated_by": current_user["id"],
            "deactivation_reason": "Deleted via API",
            "updated_at": datetime.utcnow()
        }

        orgs_collection.update_one(org_filter, {"$set": update_data})

        # Optionally deactivate all users in the organization
        if active_users > 0:
            users_collection.update_many(
                {"organization_id": org_identifier, "is_active": True},
                {"$set": {
                    "is_active": False,
                    "deactivated_at": datetime.utcnow(),
                    "deactivated_by": current_user["id"],
                    "deactivation_reason": "Organization deactivated"
                }}
            )

        return {
            "message": "Organization deactivated successfully",
            "organization_id": org_identifier,
            "organization_name": org["name"],
            "users_deactivated": active_users,
            "active_leads_count": active_leads,
            "deactivated_at": update_data["deactivated_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete organization: {str(e)}"
        )


@router.post("/api/v1/organizations/{org_id}/reactivate")
async def reactivate_organization(
        org_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Reactivate a deactivated organization"""
    try:
        # Only super admin can reactivate organizations
        if current_user.get("role") != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can reactivate organizations"
            )

        orgs_collection = db.organizations

        # Find organization
        if len(org_id) == 24:
            org = orgs_collection.find_one({"_id": ObjectId(org_id)})
            org_filter = {"_id": ObjectId(org_id)}
        else:
            org = orgs_collection.find_one({"org_id": org_id})
            org_filter = {"org_id": org_id}

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Check if already active
        if org.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Organization is already active"
            )

        # Reactivate organization
        update_data = {
            "is_active": True,
            "reactivated_at": datetime.utcnow(),
            "reactivated_by": current_user["id"],
            "updated_at": datetime.utcnow()
        }

        orgs_collection.update_one(
            org_filter,
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
            "message": "Organization reactivated successfully",
            "organization_id": org.get("org_id"),
            "organization_name": org["name"],
            "reactivated_at": update_data["reactivated_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reactivate organization: {str(e)}"
        )


@router.delete("/api/v1/organizations/{org_id}/hard-delete")
async def hard_delete_organization(
        org_id: str,
        confirm: bool = False,
        current_user: dict = Depends(get_current_user_from_token)
):
    """PERMANENTLY delete an organization and all associated data"""
    try:
        # Only super admin can hard delete organizations
        if current_user.get("role") != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can permanently delete organizations"
            )

        if not confirm:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Must set confirm=true to permanently delete organization"
            )

        orgs_collection = db.organizations
        users_collection = db.users
        leads_collection = db.leads
        projects_collection = db.projects

        # Find organization
        if len(org_id) == 24:
            org = orgs_collection.find_one({"_id": ObjectId(org_id)})
            org_filter = {"_id": ObjectId(org_id)}
        else:
            org = orgs_collection.find_one({"org_id": org_id})
            org_filter = {"org_id": org_id}

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        org_identifier = org.get("org_id")

        # Count what will be deleted
        users_count = users_collection.count_documents({"organization_id": org_identifier})
        leads_count = leads_collection.count_documents({"organization_id": org_identifier})
        projects_count = projects_collection.count_documents({"organization_id": org_identifier})

        # Delete all associated data
        users_collection.delete_many({"organization_id": org_identifier})
        leads_collection.delete_many({"organization_id": org_identifier})
        projects_collection.delete_many({"organization_id": org_identifier})

        # Delete the organization itself
        orgs_collection.delete_one(org_filter)

        return {
            "message": "Organization permanently deleted",
            "organization_id": org_identifier,
            "organization_name": org["name"],
            "deleted_data": {
                "users": users_count,
                "leads": leads_count,
                "projects": projects_count
            },
            "deleted_at": datetime.utcnow().isoformat(),
            "warning": "This action cannot be undone"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to hard delete organization: {str(e)}"
        )


# ==================== USER HARD DELETE METHOD ====================

@router.delete("/api/v1/users/{user_id}/hard-delete")
async def hard_delete_user(
        user_id: str,
        confirm: bool = False,
        current_user: dict = Depends(get_current_user_from_token)
):
    """PERMANENTLY delete a user and optionally reassign their data"""
    try:
        # Only super admin can hard delete users
        if current_user["role"] != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can permanently delete users"
            )

        if not confirm:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Must set confirm=true to permanently delete user"
            )

        users_collection = db.users
        leads_collection = db.leads

        # Find the target user
        target_user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Prevent self-deletion
        if str(target_user["_id"]) == current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot delete your own account"
            )

        username = target_user["username"]

        # Count associated data
        leads_count = leads_collection.count_documents({"created_by": username})

        # Option 1: Anonymize the user's data instead of deleting
        # Replace username with "deleted_user_[timestamp]" in all references
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        anonymous_username = f"deleted_user_{timestamp}"

        # Update leads to use anonymous username
        leads_collection.update_many(
            {"created_by": username},
            {"$set": {
                "created_by": anonymous_username,
                "original_creator": username,
                "creator_deleted_at": datetime.utcnow()
            }}
        )

        # Update any other references (approval records, etc.)
        leads_collection.update_many(
            {"approved_by": username},
            {"$set": {
                "approved_by": anonymous_username,
                "original_approver": username
            }}
        )

        # Delete the user
        users_collection.delete_one({"_id": ObjectId(user_id)})

        return {
            "message": "User permanently deleted",
            "user_id": user_id,
            "username": username,
            "anonymized_as": anonymous_username,
            "preserved_data": {
                "leads": leads_count
            },
            "deleted_at": datetime.utcnow().isoformat(),
            "warning": "This action cannot be undone"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to hard delete user: {str(e)}"
        )


# Add endpoint to upgrade organization plan
@router.put("/api/v1/organizations/{org_id}/upgrade-plan")
async def upgrade_organization_plan(
        org_id: str,
        plan_request: PlanUpgradeRequest,  # Changed to use request body
        current_user: dict = Depends(get_current_user_from_token)
):
    """Upgrade organization plan (Super Admin only)"""
    try:
        if current_user["role"] != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only super admin can upgrade organization plans"
            )

        orgs_collection = db.organizations
        org = orgs_collection.find_one({"org_id": org_id})

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Get new plan limits
        new_limits = get_organization_limits(plan_request.new_plan.value)

        # Update organization
        orgs_collection.update_one(
            {"org_id": org_id},
            {
                "$set": {
                    "plan": plan_request.new_plan.value,
                    "plan_limits": new_limits,
                    "plan_upgraded_at": datetime.utcnow(),
                    "plan_upgraded_by": current_user["username"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return {
            "message": "Organization plan upgraded successfully",
            "organization_id": org_id,
            "old_plan": org.get("plan", "basic"),
            "new_plan": plan_request.new_plan.value,
            "new_limits": new_limits
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upgrade plan: {str(e)}"
        )

# NEW: Add endpoint to serve uploaded news images
@router.get("/api/v1/news/images/{image_id}")
async def get_news_image(image_id: str):
    """Get uploaded news image"""
    try:
        news_images_collection = db.news_images
        image_doc = news_images_collection.find_one({"image_id": image_id})

        if not image_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Image not found"
            )

        # Return base64 image data
        image_data = f"data:image/jpeg;base64,{image_doc['image_data']}"

        return {
            "image_id": image_id,
            "image_data": image_data,
            "uploaded_at": image_doc["uploaded_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get image: {str(e)}"
        )


# Add endpoint to get organization limits and current usage
@router.get("/api/v1/organizations/{org_id}/limits")
async def get_organization_limits_info(
        org_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get organization limits and current usage"""
    try:
        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager"] and current_user.get(
                "organization_id") != org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view this organization's limits"
            )

        orgs_collection = db.organizations
        org = orgs_collection.find_one({"org_id": org_id})

        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        plan_limits = org.get("plan_limits", get_organization_limits("basic"))

        # Calculate current usage
        projects_collection = db.projects
        users_collection = db.users

        current_projects = projects_collection.count_documents({
            "organization_id": org_id,
            "is_active": True
        })

        current_users = users_collection.count_documents({
            "organization_id": org_id,
            "is_active": True
        })

        return {
            "organization_id": org_id,
            "organization_name": org["name"],
            "plan": org.get("plan", "basic"),
            "limits": plan_limits,
            "current_usage": {
                "projects": current_projects,
                "users": current_users
            },
            "percentage_used": {
                "projects": round((current_projects / plan_limits["max_projects"]) * 100, 2) if plan_limits[
                                                                                                    "max_projects"] > 0 else 0,
                "users": round((current_users / plan_limits["max_users"]) * 100, 2) if plan_limits[
                                                                                           "max_users"] > 0 else 0
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get organization limits: {str(e)}"
        )


@router.post("/api/v1/time-tracking/end-break")
async def end_break(
        break_end_data: BreakEnd,
        current_user: dict = Depends(get_current_user_from_token)
):
    """End the current break"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only canvassers and managers can use break tracking"
            )

        time_tracking_collection = db.time_tracking

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active session found"
            )

        # Find active break
        active_break = get_active_break(active_session)
        if not active_break:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active break found"
            )

        # Calculate break duration
        end_time = datetime.utcnow()
        duration_minutes = calculate_break_duration({
            "start_time": active_break["start_time"],
            "end_time": end_time
        })

        # Validate break duration (max 45 minutes warning)
        if duration_minutes > 45:
            warning_message = f"Break exceeded 45 minutes ({duration_minutes} minutes)"
        else:
            warning_message = None

        # Update break in session
        breaks = active_session.get("breaks", [])
        for i, break_item in enumerate(breaks):
            if break_item.get("break_id") == active_break["break_id"]:
                breaks[i].update({
                    "end_time": end_time,
                    "duration_minutes": duration_minutes,
                    "status": BreakStatus.COMPLETED.value,
                    "notes": break_end_data.notes,
                    "completed_by": current_user["username"]
                })
                break

        # Update session
        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {
                "$set": {
                    "breaks": breaks,
                    "on_break": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Get updated daily limits
        limits = validate_daily_limits(current_user["id"])

        return {
            "message": "Break ended successfully",
            "break_id": active_break["break_id"],
            "duration_minutes": duration_minutes,
            "start_time": active_break["start_time"].isoformat(),
            "end_time": end_time.isoformat(),
            "warning": warning_message,
            "daily_limits": limits
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to end break: {str(e)}"
        )


@router.get("/api/v1/time-tracking/break-status")
async def get_break_status(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current break status"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            return {"is_on_break": False}

        time_tracking_collection = db.time_tracking

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": current_user["id"],
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            return {"is_on_break": False, "is_clocked_in": False}

        # Check for active break
        active_break = get_active_break(active_session)

        if active_break:
            duration_minutes = calculate_break_duration(active_break)
            time_remaining = max(0, 45 - duration_minutes)  # 45 min max

            return {
                "is_on_break": True,
                "is_clocked_in": True,
                "break_id": active_break["break_id"],
                "break_type": active_break["break_type"],
                "start_time": active_break["start_time"].isoformat(),
                "duration_minutes": duration_minutes,
                "time_remaining_minutes": time_remaining,
                "is_overtime": duration_minutes > 45,
                "expected_duration": active_break.get("expected_duration_minutes")
            }
        else:
            # Get daily limits
            limits = validate_daily_limits(current_user["id"])

            return {
                "is_on_break": False,
                "is_clocked_in": True,
                "can_take_break": limits["can_work_more"],
                "daily_limits": limits
            }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get break status: {str(e)}"
        )


@router.get("/api/v1/time-tracking/daily-summary")
async def get_daily_summary(
        date: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get daily work and break summary"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only canvassers and managers can view daily summaries"
            )

        # Parse date or use today
        if date:
            try:
                target_date = datetime.fromisoformat(date.replace('Z', '+00:00')).date()
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid date format. Use YYYY-MM-DD"
                )
        else:
            target_date = datetime.utcnow().date()

        # Get date range
        day_start = datetime.combine(target_date, datetime.min.time())
        day_end = day_start + timedelta(days=1)

        time_tracking_collection = db.time_tracking

        # Get all sessions for the day
        sessions = list(time_tracking_collection.find({
            "user_id": current_user["id"],
            "clock_in_time": {"$gte": day_start, "$lt": day_end}
        }))

        total_work_minutes = 0
        total_break_minutes = 0
        break_details = []
        sessions_summary = []

        for session in sessions:
            session_work_minutes = 0
            session_break_minutes = 0

            # Calculate session time
            if session.get("clock_out_time"):
                session_total = (session["clock_out_time"] - session["clock_in_time"]).total_seconds() / 60
            elif session.get("is_active"):
                session_total = (datetime.utcnow() - session["clock_in_time"]).total_seconds() / 60
            else:
                session_total = 0

            # Process breaks
            session_breaks = session.get("breaks", [])
            for break_item in session_breaks:
                if break_item.get("status") in ["completed", "active"]:
                    break_duration = calculate_break_duration(break_item)
                    session_break_minutes += break_duration

                    break_details.append({
                        "break_id": break_item["break_id"],
                        "type": break_item["break_type"],
                        "start_time": break_item["start_time"].isoformat(),
                        "end_time": break_item.get("end_time").isoformat() if break_item.get("end_time") else None,
                        "duration_minutes": break_duration,
                        "status": break_item["status"],
                        "is_overtime": break_duration > 45
                    })

            session_work_minutes = max(0, session_total - session_break_minutes)

            sessions_summary.append({
                "session_id": str(session["_id"]),
                "clock_in": session["clock_in_time"].isoformat(),
                "clock_out": session.get("clock_out_time").isoformat() if session.get("clock_out_time") else None,
                "total_minutes": round(session_total, 2),
                "work_minutes": round(session_work_minutes, 2),
                "break_minutes": round(session_break_minutes, 2),
                "is_active": session.get("is_active", False)
            })

            total_work_minutes += session_work_minutes
            total_break_minutes += session_break_minutes

        return {
            "date": target_date.isoformat(),
            "summary": {
                "total_work_hours": round(total_work_minutes / 60, 2),
                "total_break_minutes": round(total_break_minutes, 2),
                "work_hours_remaining": round(max(0, (8 * 60 - total_work_minutes) / 60), 2),
                "total_breaks": len(break_details),
                "overtime_breaks": len([b for b in break_details if b["is_overtime"]])
            },
            "sessions": sessions_summary,
            "breaks": break_details,
            "limits": {
                "max_work_hours": 8,
                "max_break_minutes_per_break": 45,
                "within_limits": total_work_minutes <= 8 * 60
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get daily summary: {str(e)}"
        )
# ==================== RUN SERVER ====================
# Step 9: Enhanced active users endpoint with break information
@router.get("/api/v1/time-tracking/active-users-with-breaks")
async def get_active_users_with_breaks(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get all currently clocked-in users with break information"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view active users"
            )

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Build filter based on role
        filter_query = {
            "clock_out_time": None,
            "is_active": True
        }

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            # Get assigned canvassers
            assigned_canvassers = []
            for user in users_collection.find({"manager_id": current_user["username"]}):
                assigned_canvassers.append(user["_id"])
            filter_query["user_id"] = {"$in": [str(uid) for uid in assigned_canvassers]}

        active_users = []
        for session in time_tracking_collection.find(filter_query):
            # Get user details
            user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
            if not user:
                continue

            # Calculate elapsed time
            elapsed_time = datetime.utcnow() - session["clock_in_time"]
            elapsed_hours = elapsed_time.total_seconds() / 3600

            # Get break information
            breaks = session.get("breaks", [])
            active_break = get_active_break(session)

            total_break_minutes = sum(
                calculate_break_duration(b) for b in breaks
                if b.get("status") in ["completed", "active"]
            )

            work_hours = max(0, elapsed_hours - (total_break_minutes / 60))

            # Get latest location
            latest_location = None
            if session.get("location_points"):
                latest_location = session["location_points"][-1]

            user_data = {
                "session_id": str(session["_id"]),
                "user_id": session["user_id"],
                "username": session["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "clock_in_time": session["clock_in_time"].isoformat(),
                "elapsed_hours": round(elapsed_hours, 2),
                "work_hours": round(work_hours, 2),
                "break_minutes": round(total_break_minutes, 2),
                "latest_location": latest_location,
                "location_count": len(session.get("location_points", [])),
                "total_breaks_today": len(breaks),
                "is_on_break": bool(active_break)
            }

            if active_break:
                break_duration = calculate_break_duration(active_break)
                user_data["current_break"] = {
                    "break_id": active_break["break_id"],
                    "type": active_break["break_type"],
                    "start_time": active_break["start_time"].isoformat(),
                    "duration_minutes": break_duration,
                    "is_overtime": break_duration > 45,
                    "expected_duration": active_break.get("expected_duration_minutes")
                }

            active_users.append(user_data)

        return {
            "active_users": active_users,
            "total_count": len(active_users),
            "on_break_count": len([u for u in active_users if u["is_on_break"]]),
            "overtime_breaks": len([u for u in active_users if u.get("current_break", {}).get("is_overtime", False)])
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get active users: {str(e)}"
        )


# Step 10: Break management for managers
@router.post("/api/v1/time-tracking/force-end-break/{user_id}")
async def force_end_break(
        user_id: str,
        reason: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Allow managers to force end a user's break if it's overtime"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only managers can force end breaks"
            )

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Verify manager has permission to manage this user
        if current_user["role"] == "manager":
            target_user = users_collection.find_one({"_id": ObjectId(user_id)})
            if not target_user or target_user.get("manager_id") != current_user["username"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only manage breaks for your assigned canvassers"
                )

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": user_id,
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User has no active session"
            )

        # Find active break
        active_break = get_active_break(active_session)
        if not active_break:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is not currently on break"
            )

        # Force end the break
        end_time = datetime.utcnow()
        duration_minutes = calculate_break_duration({
            "start_time": active_break["start_time"],
            "end_time": end_time
        })

        # Update break in session
        breaks = active_session.get("breaks", [])
        for i, break_item in enumerate(breaks):
            if break_item.get("break_id") == active_break["break_id"]:
                breaks[i].update({
                    "end_time": end_time,
                    "duration_minutes": duration_minutes,
                    "status": BreakStatus.COMPLETED.value,
                    "notes": f"Force ended by manager: {reason}",
                    "completed_by": current_user["username"],
                    "force_ended": True
                })
                break

        # Update session
        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {
                "$set": {
                    "breaks": breaks,
                    "on_break": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return {
            "message": "Break force ended successfully",
            "break_id": active_break["break_id"],
            "duration_minutes": duration_minutes,
            "was_overtime": duration_minutes > 45,
            "reason": reason
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to force end break: {str(e)}"
        )


# Step 11: Break analytics for managers
@router.get("/api/v1/time-tracking/break-analytics")
async def get_break_analytics(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get break analytics for organization or specific user"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view break analytics"
            )

        # Date range setup
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.utcnow() - timedelta(days=30)  # Last 30 days

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.utcnow()

        time_tracking_collection = db.time_tracking

        # Build filter
        filter_query = {
            "clock_in_time": {"$gte": start_dt, "$lte": end_dt},
            "breaks": {"$exists": True, "$ne": []}
        }

        # Role-based filtering
        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            users_collection = db.users
            assigned_users = [u["_id"] for u in users_collection.find({"manager_id": current_user["username"]})]
            filter_query["user_id"] = {"$in": [str(uid) for uid in assigned_users]}

        if user_id:
            filter_query["user_id"] = user_id

        # Aggregate break data
        pipeline = [
            {"$match": filter_query},
            {"$unwind": "$breaks"},
            {"$match": {"breaks.status": "completed"}},
            {"$group": {
                "_id": None,
                "total_breaks": {"$sum": 1},
                "total_break_minutes": {"$sum": "$breaks.duration_minutes"},
                "avg_break_duration": {"$avg": "$breaks.duration_minutes"},
                "overtime_breaks": {
                    "$sum": {"$cond": [{"$gt": ["$breaks.duration_minutes", 45]}, 1, 0]}
                },
                "break_types": {"$push": "$breaks.break_type"},
                "max_break_duration": {"$max": "$breaks.duration_minutes"},
                "min_break_duration": {"$min": "$breaks.duration_minutes"}
            }}
        ]

        result = list(time_tracking_collection.aggregate(pipeline))

        if result:
            stats = result[0]

            # Count break types
            break_types = stats["break_types"]
            type_counts = {}
            for break_type in break_types:
                type_counts[break_type] = type_counts.get(break_type, 0) + 1

            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_breaks": stats["total_breaks"],
                    "total_break_hours": round(stats["total_break_minutes"] / 60, 2),
                    "avg_break_minutes": round(stats["avg_break_duration"], 2),
                    "overtime_breaks": stats["overtime_breaks"],
                    "overtime_percentage": round((stats["overtime_breaks"] / stats["total_breaks"]) * 100, 2),
                    "longest_break_minutes": stats["max_break_duration"],
                    "shortest_break_minutes": stats["min_break_duration"]
                },
                "break_type_distribution": type_counts,
                "compliance": {
                    "within_limit": stats["total_breaks"] - stats["overtime_breaks"],
                    "over_limit": stats["overtime_breaks"],
                    "compliance_rate": round(
                        ((stats["total_breaks"] - stats["overtime_breaks"]) / stats["total_breaks"]) * 100, 2)
                }
            }
        else:
            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_breaks": 0,
                    "total_break_hours": 0,
                    "avg_break_minutes": 0,
                    "overtime_breaks": 0,
                    "overtime_percentage": 0
                },
                "break_type_distribution": {},
                "compliance": {
                    "within_limit": 0,
                    "over_limit": 0,
                    "compliance_rate": 100
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get break analytics: {str(e)}"
        )


# Step 9: Enhanced active users endpoint with break information
@router.get("/api/v1/time-tracking/active-users-with-breaks")
async def get_active_users_with_breaks(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get all currently clocked-in users with break information"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view active users"
            )

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Build filter based on role
        filter_query = {
            "clock_out_time": None,
            "is_active": True
        }

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            # Get assigned canvassers
            assigned_canvassers = []
            for user in users_collection.find({"manager_id": current_user["username"]}):
                assigned_canvassers.append(user["_id"])
            filter_query["user_id"] = {"$in": [str(uid) for uid in assigned_canvassers]}

        active_users = []
        for session in time_tracking_collection.find(filter_query):
            # Get user details
            user = users_collection.find_one({"_id": ObjectId(session["user_id"])})
            if not user:
                continue

            # Calculate elapsed time
            elapsed_time = datetime.utcnow() - session["clock_in_time"]
            elapsed_hours = elapsed_time.total_seconds() / 3600

            # Get break information
            breaks = session.get("breaks", [])
            active_break = get_active_break(session)

            total_break_minutes = sum(
                calculate_break_duration(b) for b in breaks
                if b.get("status") in ["completed", "active"]
            )

            work_hours = max(0, elapsed_hours - (total_break_minutes / 60))

            # Get latest location
            latest_location = None
            if session.get("location_points"):
                latest_location = session["location_points"][-1]

            user_data = {
                "session_id": str(session["_id"]),
                "user_id": session["user_id"],
                "username": session["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "clock_in_time": session["clock_in_time"].isoformat(),
                "elapsed_hours": round(elapsed_hours, 2),
                "work_hours": round(work_hours, 2),
                "break_minutes": round(total_break_minutes, 2),
                "latest_location": latest_location,
                "location_count": len(session.get("location_points", [])),
                "total_breaks_today": len(breaks),
                "is_on_break": bool(active_break)
            }

            if active_break:
                break_duration = calculate_break_duration(active_break)
                user_data["current_break"] = {
                    "break_id": active_break["break_id"],
                    "type": active_break["break_type"],
                    "start_time": active_break["start_time"].isoformat(),
                    "duration_minutes": break_duration,
                    "is_overtime": break_duration > 45,
                    "expected_duration": active_break.get("expected_duration_minutes")
                }

            active_users.append(user_data)

        return {
            "active_users": active_users,
            "total_count": len(active_users),
            "on_break_count": len([u for u in active_users if u["is_on_break"]]),
            "overtime_breaks": len([u for u in active_users if u.get("current_break", {}).get("is_overtime", False)])
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get active users: {str(e)}"
        )


# Step 10: Break management for managers
@router.post("/api/v1/time-tracking/force-end-break/{user_id}")
async def force_end_break(
        user_id: str,
        reason: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Allow managers to force end a user's break if it's overtime"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only managers can force end breaks"
            )

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Verify manager has permission to manage this user
        if current_user["role"] == "manager":
            target_user = users_collection.find_one({"_id": ObjectId(user_id)})
            if not target_user or target_user.get("manager_id") != current_user["username"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only manage breaks for your assigned canvassers"
                )

        # Find active session
        active_session = time_tracking_collection.find_one({
            "user_id": user_id,
            "clock_out_time": None,
            "is_active": True
        })

        if not active_session:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User has no active session"
            )

        # Find active break
        active_break = get_active_break(active_session)
        if not active_break:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User is not currently on break"
            )

        # Force end the break
        end_time = datetime.utcnow()
        duration_minutes = calculate_break_duration({
            "start_time": active_break["start_time"],
            "end_time": end_time
        })

        # Update break in session
        breaks = active_session.get("breaks", [])
        for i, break_item in enumerate(breaks):
            if break_item.get("break_id") == active_break["break_id"]:
                breaks[i].update({
                    "end_time": end_time,
                    "duration_minutes": duration_minutes,
                    "status": BreakStatus.COMPLETED.value,
                    "notes": f"Force ended by manager: {reason}",
                    "completed_by": current_user["username"],
                    "force_ended": True
                })
                break

        # Update session
        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            {
                "$set": {
                    "breaks": breaks,
                    "on_break": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return {
            "message": "Break force ended successfully",
            "break_id": active_break["break_id"],
            "duration_minutes": duration_minutes,
            "was_overtime": duration_minutes > 45,
            "reason": reason
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to force end break: {str(e)}"
        )


# Step 11: Break analytics for managers
@router.get("/api/v1/time-tracking/break-analytics")
async def get_break_analytics(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get break analytics for organization or specific user"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view break analytics"
            )

        # Date range setup
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.utcnow() - timedelta(days=30)  # Last 30 days

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.utcnow()

        time_tracking_collection = db.time_tracking

        # Build filter
        filter_query = {
            "clock_in_time": {"$gte": start_dt, "$lte": end_dt},
            "breaks": {"$exists": True, "$ne": []}
        }

        # Role-based filtering
        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            users_collection = db.users
            assigned_users = [u["_id"] for u in users_collection.find({"manager_id": current_user["username"]})]
            filter_query["user_id"] = {"$in": [str(uid) for uid in assigned_users]}

        if user_id:
            filter_query["user_id"] = user_id

        # Aggregate break data
        pipeline = [
            {"$match": filter_query},
            {"$unwind": "$breaks"},
            {"$match": {"breaks.status": "completed"}},
            {"$group": {
                "_id": None,
                "total_breaks": {"$sum": 1},
                "total_break_minutes": {"$sum": "$breaks.duration_minutes"},
                "avg_break_duration": {"$avg": "$breaks.duration_minutes"},
                "overtime_breaks": {
                    "$sum": {"$cond": [{"$gt": ["$breaks.duration_minutes", 45]}, 1, 0]}
                },
                "break_types": {"$push": "$breaks.break_type"},
                "max_break_duration": {"$max": "$breaks.duration_minutes"},
                "min_break_duration": {"$min": "$breaks.duration_minutes"}
            }}
        ]

        result = list(time_tracking_collection.aggregate(pipeline))

        if result:
            stats = result[0]

            # Count break types
            break_types = stats["break_types"]
            type_counts = {}
            for break_type in break_types:
                type_counts[break_type] = type_counts.get(break_type, 0) + 1

            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_breaks": stats["total_breaks"],
                    "total_break_hours": round(stats["total_break_minutes"] / 60, 2),
                    "avg_break_minutes": round(stats["avg_break_duration"], 2),
                    "overtime_breaks": stats["overtime_breaks"],
                    "overtime_percentage": round((stats["overtime_breaks"] / stats["total_breaks"]) * 100, 2),
                    "longest_break_minutes": stats["max_break_duration"],
                    "shortest_break_minutes": stats["min_break_duration"]
                },
                "break_type_distribution": type_counts,
                "compliance": {
                    "within_limit": stats["total_breaks"] - stats["overtime_breaks"],
                    "over_limit": stats["overtime_breaks"],
                    "compliance_rate": round(
                        ((stats["total_breaks"] - stats["overtime_breaks"]) / stats["total_breaks"]) * 100, 2)
                }
            }
        else:
            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_breaks": 0,
                    "total_break_hours": 0,
                    "avg_break_minutes": 0,
                    "overtime_breaks": 0,
                    "overtime_percentage": 0
                },
                "break_type_distribution": {},
                "compliance": {
                    "within_limit": 0,
                    "over_limit": 0,
                    "compliance_rate": 100
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get break analytics: {str(e)}"
        )


# ==================== COMPETITION MANAGEMENT ====================
# REPLACE the existing create_competition endpoint:


