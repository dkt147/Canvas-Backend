"""
Competitions endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Competitions'])

@router.post("/api/v1/competitions")
async def create_competition(
        competition_data: CompetitionCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new organization-specific competition with participant selection"""
    try:
        if not check_competition_permission(current_user, "create"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to create competitions"
            )

        # Validate dates
        if competition_data.end_date <= competition_data.start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="End date must be after start date"
            )

        # MUST have organization (no more global competitions)
        if not current_user.get("organization_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You must belong to an organization to create competitions"
            )

        # Validate participant selection
        if competition_data.participant_selection_mode == "specific":
            if not competition_data.selected_participants or len(
                    competition_data.selected_participants) < competition_data.min_participants:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Must select at least {competition_data.min_participants} participants"
                )

            # Verify all selected participants exist in organization
            users_collection = db.users
            valid_usernames = []
            for username in competition_data.selected_participants:
                user = users_collection.find_one({
                    "username": username,
                    "organization_id": current_user["organization_id"],
                    "is_active": True
                })
                if not user:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"User '{username}' not found in your organization"
                    )
                valid_usernames.append(username)

            competition_data.selected_participants = valid_usernames

        competitions_collection = db.competitions

        # Generate competition ID
        org_id = current_user["organization_id"]
        competition_id = generate_competition_id(org_id)

        # Determine status based on dates
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if competition_data.start_date > now:
            status_value = CompetitionStatus.UPCOMING.value
        elif competition_data.end_date < now:
            status_value = CompetitionStatus.COMPLETED.value
        else:
            status_value = CompetitionStatus.ACTIVE.value

        # Create competition document
        competition_doc = {
            "competition_id": competition_id,
            "title": competition_data.title,
            "description": competition_data.description,
            "competition_type": competition_data.competition_type.value,
            "start_date": competition_data.start_date,
            "end_date": competition_data.end_date,
            "prize_description": competition_data.prize_description,
            "prize_points": competition_data.prize_points,
            "target_roles": competition_data.target_roles,
            "organization_id": org_id,  # Always organization-specific
            "min_participants": competition_data.min_participants,
            "status": status_value,
            "is_active": competition_data.is_active,
            # NEW FIELDS
            "participant_selection_mode": competition_data.participant_selection_mode,
            "selected_participants": competition_data.selected_participants,
            # Metadata
            "created_by": current_user["username"],
            "created_at": datetime.utcnow(),
            "winner": None
        }

        result = competitions_collection.insert_one(competition_doc)

        # Get initial participant count
        participants = get_competition_participants(competition_doc)

        if len(participants) < competition_data.min_participants:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Not enough eligible participants. Found {len(participants)}, need {competition_data.min_participants}"
            )

        # Send notifications to participants
        if participants:
            participant_usernames = [p["username"] for p in participants]
            create_notification({
                "title": "🏆 New Competition Started!",
                "message": f"You've been invited to compete in '{competition_data.title}'",
                "type": NotificationType.COMPETITION_UPDATE.value,
                "recipient_usernames": participant_usernames,
                "priority": "high",
                "data": {
                    "competition_id": competition_id,
                    "start_date": competition_data.start_date.isoformat(),
                    "end_date": competition_data.end_date.isoformat()
                }
            })

        return {
            "message": "Competition created successfully",
            "competition_id": competition_id,
            "database_id": str(result.inserted_id),
            "status": status_value,
            "participant_count": len(participants),
            "participant_selection_mode": competition_data.participant_selection_mode,
            "organization_id": org_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create competition: {str(e)}"
        )


# REPLACE the existing list_competitions endpoint:

@router.get("/api/v1/competitions")
async def list_competitions(
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List organization-specific competitions"""
    try:
        competitions_collection = db.competitions

        # Build filter - ONLY organization competitions
        filter_query = {"is_active": True}

        # Must have organization
        if not current_user.get("organization_id"):
            return {
                "competitions": [],
                "pagination": {
                    "current_page": page,
                    "total_pages": 0,
                    "total_count": 0,
                    "page_size": limit
                }
            }

        # Filter by organization
        if current_user["role"] == "super_admin":
            # Super admin can see all, but still filter by their selected org if they have one
            if current_user.get("organization_id"):
                filter_query["organization_id"] = current_user["organization_id"]
        else:
            filter_query["organization_id"] = current_user["organization_id"]

        if status:
            filter_query["status"] = status

        # Count total
        total_count = competitions_collection.count_documents(filter_query)

        # Pagination
        skip = (page - 1) * limit

        competitions = []
        for comp in competitions_collection.find(filter_query).skip(skip).limit(limit).sort("created_at", -1):
            # Get creator info
            users_collection = db.users
            creator = users_collection.find_one({"username": comp["created_by"]})
            creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
            comp["created_by"]

            # Get participants
            participants = get_competition_participants(comp)

            # Check if current user is eligible
            user_eligible = any(p["username"] == current_user["username"] for p in participants)

            competitions.append({
                "id": str(comp["_id"]),
                "competition_id": comp["competition_id"],
                "title": comp["title"],
                "description": comp["description"],
                "competition_type": comp["competition_type"],
                "start_date": comp["start_date"].isoformat(),
                "end_date": comp["end_date"].isoformat(),
                "prize_description": comp["prize_description"],
                "prize_points": comp["prize_points"],
                "status": comp["status"],
                "target_roles": comp["target_roles"],
                "participant_selection_mode": comp.get("participant_selection_mode", "all"),
                "created_by": comp["created_by"],
                "created_by_name": creator_name,
                "created_at": comp["created_at"].isoformat(),
                "participant_count": len(participants),
                "user_eligible": user_eligible,
                "user_is_participant": user_eligible,
                "organization_id": comp.get("organization_id")
            })

        return {
            "competitions": competitions,
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
            detail=f"Failed to list competitions: {str(e)}"
        )

# ADD this new endpoint to help admins select participants:

@router.get("/api/v1/competitions/available-participants")
async def get_available_participants(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get list of users available for competition selection"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can view available participants"
            )

        if not current_user.get("organization_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You must belong to an organization"
            )

        users_collection = db.users

        # Get all active users in organization
        users = list(users_collection.find({
            "organization_id": current_user["organization_id"],
            "is_active": True
        }).sort("username", 1))

        # Group by role
        participants_by_role = {}
        all_participants = []

        for user in users:
            participant = {
                "user_id": str(user["_id"]),
                "username": user["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"],
                "role": user["role"],
                "points": user.get("points", 0)
            }

            all_participants.append(participant)

            role = user["role"]
            if role not in participants_by_role:
                participants_by_role[role] = []
            participants_by_role[role].append(participant)

        return {
            "all_participants": all_participants,
            "by_role": participants_by_role,
            "total_count": len(all_participants),
            "organization_id": current_user["organization_id"]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get available participants: {str(e)}"
        )

# ADD this new endpoint to get competition details with participants:

@router.get("/api/v1/competitions/{competition_id}")
async def get_competition_details(
        competition_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get detailed competition information"""
    try:
        competitions_collection = db.competitions

        # Find competition
        competition = competitions_collection.find_one({"competition_id": competition_id})
        if not competition:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Competition not found"
            )

        # Check access
        if current_user["role"] != "super_admin":
            if competition.get("organization_id") != current_user.get("organization_id"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to this competition"
                )

        # Get participants
        participants = get_competition_participants(competition)

        # Check if user is participant
        user_is_participant = any(p["username"] == current_user["username"] for p in participants)

        return {
            "competition_id": competition["competition_id"],
            "title": competition["title"],
            "description": competition["description"],
            "competition_type": competition["competition_type"],
            "status": competition["status"],
            "start_date": competition["start_date"].isoformat(),
            "end_date": competition["end_date"].isoformat(),
            "prize_description": competition["prize_description"],
            "prize_points": competition["prize_points"],
            "target_roles": competition["target_roles"],
            "participant_selection_mode": competition.get("participant_selection_mode", "all"),
            "selected_participants": competition.get("selected_participants"),
            "participants": participants,
            "participant_count": len(participants),
            "user_is_participant": user_is_participant,
            "organization_id": competition.get("organization_id"),
            "created_by": competition["created_by"],
            "created_at": competition["created_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get competition details: {str(e)}"
        )

# REPLACE the existing get_competition_leaderboard_enhanced endpoint:

@router.get("/api/v1/competitions/{competition_id}/leaderboard")
async def get_competition_leaderboard_enhanced(
        competition_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get competition leaderboard - ENHANCED VERSION"""
    try:
        competitions_collection = db.competitions

        # Find competition
        competition = competitions_collection.find_one({"competition_id": competition_id})
        if not competition:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Competition not found"
            )

        # Check access - must be in same organization
        if current_user["role"] != "super_admin":
            if competition.get("organization_id") != current_user.get("organization_id"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You don't have access to this competition"
                )

        # Get participants based on selection mode
        participants = get_competition_participants(competition)

        if not participants:
            return {
                "competition_id": competition_id,
                "title": competition["title"],
                "description": competition["description"],
                "competition_type": competition["competition_type"],
                "status": competition["status"],
                "start_date": competition["start_date"].isoformat(),
                "end_date": competition["end_date"].isoformat(),
                "prize_description": competition["prize_description"],
                "prize_points": competition["prize_points"],
                "participant_selection_mode": competition.get("participant_selection_mode", "all"),
                "organization_id": competition.get("organization_id"),
                "leaderboard": [],
                "total_participants": 0,
                "winner": None,
                "my_position": None,
                "message": "No eligible participants found"
            }

        # Calculate stats
        stats = calculate_competition_stats(competition, participants)

        # Find current user's position
        user_position = None
        user_is_participant = False
        for participant in stats["leaderboard"]:
            if participant["username"] == current_user["username"]:
                user_position = participant
                user_is_participant = True
                break

        # Check if competition should be completed
        if competition["status"] == "active" and datetime.utcnow() > competition["end_date"]:
            # Update competition status
            competitions_collection.update_one(
                {"competition_id": competition_id},
                {"$set": {
                    "status": CompetitionStatus.COMPLETED.value,
                    "completed_at": datetime.utcnow(),
                    "final_leaderboard": stats["leaderboard"]
                }}
            )

            # Award points to winner
            if stats["winner"] and competition["prize_points"] > 0:
                users_collection = db.users
                users_collection.update_one(
                    {"username": stats["winner"]["username"]},
                    {
                        "$inc": {"points": competition["prize_points"]},
                        "$push": {
                            "points_history": {
                                "action": "add",
                                "points": competition["prize_points"],
                                "reason": f"Won competition: {competition['title']}",
                                "timestamp": datetime.utcnow()
                            }
                        }
                    }
                )

                # Notify winner
                create_notification({
                    "title": "🎉 Competition Winner!",
                    "message": f"Congratulations! You won '{competition['title']}' and earned {competition['prize_points']} points!",
                    "type": NotificationType.COMPETITION_UPDATE.value,
                    "recipient_usernames": [stats["winner"]["username"]],
                    "priority": "urgent"
                })

                # Notify all other participants
                other_participants = [p["username"] for p in participants if p["username"] != stats["winner"]["username"]]
                if other_participants:
                    create_notification({
                        "title": "🏆 Competition Completed",
                        "message": f"Competition '{competition['title']}' has ended. Winner: {stats['winner']['name']}",
                        "type": NotificationType.COMPETITION_UPDATE.value,
                        "recipient_usernames": other_participants,
                        "priority": "normal"
                    })

        return {
            "competition_id": competition_id,
            "title": competition["title"],
            "description": competition["description"],
            "competition_type": competition["competition_type"],
            "status": competition["status"],
            "start_date": competition["start_date"].isoformat(),
            "end_date": competition["end_date"].isoformat(),
            "prize_description": competition["prize_description"],
            "prize_points": competition["prize_points"],
            "participant_selection_mode": competition.get("participant_selection_mode", "all"),
            "selected_participants_count": len(competition.get("selected_participants", [])) if competition.get("participant_selection_mode") == "specific" else None,
            "organization_id": competition.get("organization_id"),
            "leaderboard": stats["leaderboard"],
            "total_participants": stats["total_participants"],
            "winner": stats["winner"],
            "my_position": user_position,
            "user_is_participant": user_is_participant
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get leaderboard: {str(e)}"
        )

@router.get("/api/v1/leaderboard/live-leads")
async def get_live_leads_leaderboard(
        period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """
    Get LIVE LEADS leaderboard (separate from competition leaderboard)
    Shows real-time ranking by leads created in period
    """
    try:
        users_collection = db.users
        leads_collection = db.leads

        # Calculate date range based on period
        now = datetime.utcnow()
        if period == "daily":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_label = "Today"
        elif period == "weekly":
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            period_label = "This Week"
        elif period == "monthly":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            period_label = "This Month"

        # Build filter for organization
        filter_query = {
            "created_at": {"$gte": start, "$lte": now},
            "is_active": True
        }

        if current_user["role"] != "super_admin":
            if not current_user.get("organization_id"):
                return {
                    "period": period,
                    "period_label": period_label,
                    "leaderboard": [],
                    "total_users": 0,
                    "my_position": None
                }
            filter_query["organization_id"] = current_user.get("organization_id")

        # Aggregate lead counts by user with status breakdown
        pipeline = [
            {"$match": filter_query},
            {"$group": {
                "_id": "$created_by",
                "total_leads": {"$sum": 1},
                "approved_leads": {
                    "$sum": {"$cond": [{"$eq": ["$lead_status", "approved"]}, 1, 0]}
                },
                "sold_leads": {
                    "$sum": {"$cond": [{"$eq": ["$lead_status", "sold"]}, 1, 0]}
                },
                "pending_leads": {
                    "$sum": {"$cond": [{"$eq": ["$lead_status", "pending"]}, 1, 0]}
                }
            }},
            {"$sort": {"total_leads": -1}},
            {"$limit": limit}
        ]

        results = list(leads_collection.aggregate(pipeline))

        # Build leaderboard with user info
        leaderboard = []
        user_position = None

        for i, row in enumerate(results, 1):
            user = users_collection.find_one({"username": row["_id"]})
            if user:
                entry = {
                    "rank": i,
                    "username": user["username"],
                    "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"],
                    "total_leads": row["total_leads"],
                    "approved_leads": row["approved_leads"],
                    "sold_leads": row["sold_leads"],
                    "pending_leads": row["pending_leads"],
                    "role": user["role"],
                    "points": user.get("points", 0)
                }
                leaderboard.append(entry)

                if user["username"] == current_user["username"]:
                    user_position = entry

        return {
            "period": period,
            "period_label": period_label,
            "start_date": start.isoformat(),
            "end_date": now.isoformat(),
            "leaderboard": leaderboard,
            "total_users": len(leaderboard),
            "my_position": user_position,
            "organization_id": current_user.get("organization_id")
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get live leads leaderboard: {str(e)}"
        )


# ==================== NOTIFICATION ENDPOINTS ====================

@router.get("/api/v1/notifications")
async def get_my_notifications(
        unread_only: bool = False,
        limit: int = 50,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get notifications for current user"""
    try:
        notifications_collection = db.notifications

        filter_query = {
            "recipient_usernames": current_user["username"],
            "expires_at": {"$gt": datetime.utcnow()}
        }

        if unread_only:
            filter_query["is_read"] = False

        notifications = []
        for notif in notifications_collection.find(filter_query).sort("created_at", -1).limit(limit):
            notifications.append({
                "id": str(notif["_id"]),
                "notification_id": notif["notification_id"],
                "title": notif["title"],
                "message": notif["message"],
                "type": notif["type"],
                "priority": notif["priority"],
                "data": notif.get("data"),
                "is_read": notif["is_read"],
                "created_at": notif["created_at"].isoformat()
            })

        unread_count = notifications_collection.count_documents({
            "recipient_usernames": current_user["username"],
            "is_read": False,
            "expires_at": {"$gt": datetime.utcnow()}
        })

        return {
            "notifications": notifications,
            "total": len(notifications),
            "unread_count": unread_count
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get notifications: {str(e)}"
        )


@router.post("/api/v1/notifications/{notification_id}/mark-read")
async def mark_notification_read(
        notification_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Mark notification as read"""
    try:
        notifications_collection = db.notifications

        result = notifications_collection.update_one(
            {
                "notification_id": notification_id,
                "recipient_usernames": current_user["username"]
            },
            {
                "$set": {
                    "is_read": True,
                    "read_at": datetime.utcnow()
                }
            }
        )

        if result.matched_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Notification not found"
            )

        return {"message": "Notification marked as read"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark notification as read: {str(e)}"
        )


# REPLACE the existing update_competition endpoint:

@router.put("/api/v1/competitions/{competition_id}")
async def update_competition(
        competition_id: str,
        competition_update: CompetitionUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update a competition"""
    try:
        if not check_competition_permission(current_user, "update"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to update competitions"
            )

        competitions_collection = db.competitions

        # Find competition
        competition = competitions_collection.find_one({"competition_id": competition_id})
        if not competition:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Competition not found"
            )

        # Check organization access
        if current_user["role"] != "super_admin":
            if competition.get("organization_id") != current_user.get("organization_id"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only update competitions in your organization"
                )

        # Check if competition can be updated
        if competition["status"] == "completed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update completed competition"
            )

        # Build update document
        update_data = {}

        if competition_update.title is not None:
            update_data["title"] = competition_update.title
        if competition_update.description is not None:
            update_data["description"] = competition_update.description
        if competition_update.start_date is not None:
            update_data["start_date"] = competition_update.start_date
        if competition_update.end_date is not None:
            update_data["end_date"] = competition_update.end_date
        if competition_update.prize_description is not None:
            update_data["prize_description"] = competition_update.prize_description
        if competition_update.prize_points is not None:
            update_data["prize_points"] = competition_update.prize_points
        if competition_update.is_active is not None:
            update_data["is_active"] = competition_update.is_active

        # Handle participant selection updates
        if competition_update.participant_selection_mode is not None:
            update_data["participant_selection_mode"] = competition_update.participant_selection_mode

        if competition_update.selected_participants is not None:
            # Validate participants if mode is specific
            if competition_update.participant_selection_mode == "specific" or competition.get(
                    "participant_selection_mode") == "specific":
                users_collection = db.users
                valid_usernames = []

                for username in competition_update.selected_participants:
                    user = users_collection.find_one({
                        "username": username,
                        "organization_id": competition["organization_id"],
                        "is_active": True
                    })
                    if not user:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"User '{username}' not found in organization"
                        )
                    valid_usernames.append(username)

                update_data["selected_participants"] = valid_usernames
            else:
                update_data["selected_participants"] = competition_update.selected_participants

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )

        update_data["updated_at"] = datetime.utcnow()
        update_data["updated_by"] = current_user["username"]

        # Perform update
        competitions_collection.update_one(
            {"competition_id": competition_id},
            {"$set": update_data}
        )

        # If participants changed, notify them
        if "selected_participants" in update_data or "participant_selection_mode" in update_data:
            updated_comp = competitions_collection.find_one({"competition_id": competition_id})
            participants = get_competition_participants(updated_comp)

            if participants:
                participant_usernames = [p["username"] for p in participants]
                create_notification({
                    "title": "📢 Competition Updated",
                    "message": f"Competition '{updated_comp['title']}' has been updated",
                    "type": NotificationType.COMPETITION_UPDATE.value,
                    "recipient_usernames": participant_usernames,
                    "priority": "normal"
                })

        return {
            "message": "Competition updated successfully",
            "competition_id": competition_id,
            "updated_fields": list(update_data.keys())
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update competition: {str(e)}"
        )

@router.delete("/api/v1/competitions/{competition_id}")
async def delete_competition(
        competition_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Delete a competition (soft delete)"""
    try:
        if not check_competition_permission(current_user, "delete"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to delete competitions"
            )

        competitions_collection = db.competitions

        # Find competition
        competition = competitions_collection.find_one({"competition_id": competition_id})
        if not competition:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Competition not found"
            )

        # Soft delete
        competitions_collection.update_one(
            {"competition_id": competition_id},
            {"$set": {
                "is_active": False,
                "deleted_at": datetime.utcnow(),
                "deleted_by": current_user["username"]
            }}
        )

        return {
            "message": "Competition deleted successfully",
            "competition_id": competition_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete competition: {str(e)}"
        )


@router.get("/api/v1/competitions/debug")
async def debug_competitions(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Debug endpoint to check competition visibility"""
    try:
        competitions_collection = db.competitions

        all_competitions = list(competitions_collection.find({"is_active": True}))

        debug_info = {
            "user_info": {
                "username": current_user["username"],
                "role": current_user["role"],
                "organization_id": current_user.get("organization_id")
            },
            "all_competitions": []
        }

        for comp in all_competitions:
            is_eligible = current_user["role"] in comp["target_roles"]
            org_match = (comp.get("organization_id") is None or
                         comp.get("organization_id") == current_user.get("organization_id"))

            debug_info["all_competitions"].append({
                "competition_id": comp["competition_id"],
                "title": comp["title"],
                "target_roles": comp["target_roles"],
                "organization_id": comp.get("organization_id"),
                "status": comp["status"],
                "role_eligible": is_eligible,
                "org_eligible": org_match,
                "should_see": is_eligible and org_match
            })

        return debug_info

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Debug failed: {str(e)}"
        )

@router.get("/api/v1/competitions/my-stats")
async def get_my_competition_stats(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current user's competition statistics"""
    try:
        competitions_collection = db.competitions

        # Find active competitions user is eligible for
        now = datetime.utcnow()

        # FIX: Improve the query structure
        match_query = {
            "status": "active",
            "start_date": {"$lte": now},
            "end_date": {"$gte": now},
            "target_roles": current_user["role"],
            "is_active": True
        }

        # Add organization filter
        org_filter = [
            {"organization_id": None},  # Global competitions
        ]

        if current_user.get("organization_id"):
            org_filter.append({"organization_id": current_user["organization_id"]})

        match_query["$or"] = org_filter

        active_competitions = list(competitions_collection.find(match_query))

        my_stats = []
        for comp in active_competitions:
            participants = get_competition_participants(comp)
            stats = calculate_competition_stats(comp, participants)

            # Find user's position
            user_position = None
            for participant in stats["leaderboard"]:
                if participant["username"] == current_user["username"]:
                    user_position = participant
                    break

            if user_position:
                my_stats.append({
                    "competition_id": comp["competition_id"],
                    "title": comp["title"],
                    "competition_type": comp["competition_type"],
                    "end_date": comp["end_date"].isoformat(),
                    "my_rank": user_position["rank"],
                    "my_score": user_position["score"],
                    "metric": user_position["metric"],
                    "total_participants": stats["total_participants"],
                    "leader_score": stats["leaderboard"][0]["score"] if stats["leaderboard"] else 0
                })

        return {
            "active_competitions": len(my_stats),
            "my_competition_stats": my_stats
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get competition stats: {str(e)}"
        )


@router.post("/api/v1/live-tracking/update-location-enhanced")
async def update_location_enhanced(
        tracking_data: LiveTrackingUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Enhanced location update with path tracking"""
    try:
        if current_user["role"] not in ["canvasser", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Location tracking only available for canvassers and managers"
            )

        time_tracking_collection = db.time_tracking
        live_tracking_collection = db.live_tracking

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

        # Set timestamp if not provided
        if not tracking_data.location.timestamp:
            tracking_data.location.timestamp = datetime.utcnow()

        # Get recent location points for this session
        recent_points = active_session.get("location_points", [])

        # Detect activity type if not provided
        if not tracking_data.activity_type or tracking_data.activity_type == "moving":
            tracking_data.activity_type = detect_activity_type(
                tracking_data.location.dict(), recent_points
            )

        # Create location point with enhanced data
        location_point = {
            "latitude": tracking_data.location.latitude,
            "longitude": tracking_data.location.longitude,
            "accuracy": tracking_data.location.accuracy,
            "speed": tracking_data.location.speed,
            "heading": tracking_data.location.heading,
            "altitude": tracking_data.location.altitude,
            "timestamp": tracking_data.location.timestamp,
            "activity_type": tracking_data.activity_type,
            "notes": tracking_data.notes
        }

        # Calculate path segment if there's a previous point
        path_segment = None
        if recent_points:
            last_point = recent_points[-1]
            # Only create segment if points are more than 30 seconds apart
            time_diff = (location_point["timestamp"] - last_point["timestamp"]).total_seconds()
            if time_diff >= 30:  # 30 seconds minimum interval
                path_segment = create_path_segment(last_point, location_point)

        # Update time tracking session
        update_data = {"$push": {"location_points": location_point}}
        if path_segment:
            update_data["$push"]["path_segments"] = path_segment

        time_tracking_collection.update_one(
            {"_id": active_session["_id"]},
            update_data
        )

        # Store in live tracking collection for real-time access
        live_tracking_doc = {
            "user_id": current_user["id"],
            "username": current_user["username"],
            "session_id": str(active_session["_id"]),
            "organization_id": current_user["organization_id"],
            "location": location_point,
            "path_segment": path_segment,
            "is_active": True,
            "last_update": datetime.utcnow()
        }

        # Upsert live tracking document
        live_tracking_collection.update_one(
            {"user_id": current_user["id"], "is_active": True},
            {"$set": live_tracking_doc},
            upsert=True
        )

        return {
            "message": "Location updated successfully",
            "timestamp": location_point["timestamp"].isoformat(),
            "activity_type": tracking_data.activity_type,
            "path_segment_created": path_segment is not None,
            "total_points": len(recent_points) + 1
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update location: {str(e)}"
        )


@router.get("/api/v1/live-tracking/current-paths")
async def get_current_paths(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current live paths of all active users"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view live tracking"
            )

        live_tracking_collection = db.live_tracking
        users_collection = db.users

        # Build filter based on role
        filter_query = {"is_active": True}

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            # Get assigned canvassers
            assigned_users = []
            for user in users_collection.find({"manager_id": current_user["username"]}):
                assigned_users.append(str(user["_id"]))
            filter_query["user_id"] = {"$in": assigned_users}

        # Get live tracking data
        live_paths = []
        for tracking in live_tracking_collection.find(filter_query):
            # Get user details
            user = users_collection.find_one({"_id": ObjectId(tracking["user_id"])})
            if not user:
                continue

            # Calculate time since last update
            last_update = tracking.get("last_update", tracking["location"]["timestamp"])
            time_since_update = (datetime.utcnow() - last_update).total_seconds()

            live_paths.append({
                "user_id": tracking["user_id"],
                "username": tracking["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "current_location": tracking["location"],
                "last_update": last_update.isoformat(),
                "seconds_since_update": int(time_since_update),
                "is_recent": time_since_update < 300,  # 5 minutes
                "session_id": tracking["session_id"]
            })

        return {
            "live_paths": live_paths,
            "total_active": len(live_paths),
            "recent_updates": len([p for p in live_paths if p["is_recent"]])
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get current paths: {str(e)}"
        )


@router.get("/api/v1/live-tracking/user-path/{user_id}")
async def get_user_complete_path(
        user_id: str,
        date: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get complete path for a specific user on a specific date"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            if current_user["id"] != user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only view your own path"
                )

        # Parse date or use today
        if date:
            target_date = datetime.fromisoformat(date.replace('Z', '+00:00')).date()
        else:
            target_date = datetime.utcnow().date()

        day_start = datetime.combine(target_date, datetime.min.time())
        day_end = day_start + timedelta(days=1)

        time_tracking_collection = db.time_tracking
        users_collection = db.users

        # Get user info
        user = users_collection.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Get all sessions for the day
        sessions = list(time_tracking_collection.find({
            "user_id": user_id,
            "clock_in_time": {"$gte": day_start, "$lt": day_end}
        }).sort("clock_in_time", 1))

        complete_path = {
            "user_id": user_id,
            "username": user["username"],
            "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "date": target_date.isoformat(),
            "sessions": []
        }

        total_distance = 0
        total_duration = 0

        for session in sessions:
            location_points = session.get("location_points", [])
            path_segments = session.get("path_segments", [])

            session_distance = sum(seg.get("distance_meters", 0) for seg in path_segments)
            session_duration = (session.get("clock_out_time", datetime.utcnow()) -
                                session["clock_in_time"]).total_seconds()

            complete_path["sessions"].append({
                "session_id": str(session["_id"]),
                "clock_in_time": session["clock_in_time"].isoformat(),
                "clock_out_time": session.get("clock_out_time").isoformat() if session.get("clock_out_time") else None,
                "location_points": location_points,
                "path_segments": path_segments,
                "session_distance_meters": session_distance,
                "session_duration_seconds": session_duration,
                "total_points": len(location_points),
                "is_active": session.get("is_active", False)
            })

            total_distance += session_distance
            total_duration += session_duration

        complete_path["summary"] = {
            "total_sessions": len(sessions),
            "total_distance_meters": total_distance,
            "total_distance_km": round(total_distance / 1000, 2),
            "total_duration_hours": round(total_duration / 3600, 2),
            "average_speed_kmh": round(calculate_speed(total_distance, total_duration), 2) if total_duration > 0 else 0
        }

        return complete_path

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user path: {str(e)}"
        )


@router.get("/api/v1/live-tracking/path-analytics")
async def get_path_analytics(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_id: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get path analytics and movement patterns"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view path analytics"
            )

        # Date range setup
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.utcnow() - timedelta(days=7)

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.utcnow()

        time_tracking_collection = db.time_tracking

        # Build filter
        filter_query = {
            "clock_in_time": {"$gte": start_dt, "$lte": end_dt},
            "path_segments": {"$exists": True, "$ne": []}
        }

        # Role-based filtering
        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]

        if user_id:
            filter_query["user_id"] = user_id

        # Aggregate path data
        pipeline = [
            {"$match": filter_query},
            {"$unwind": "$path_segments"},
            {"$group": {
                "_id": None,
                "total_distance": {"$sum": "$path_segments.distance_meters"},
                "total_segments": {"$sum": 1},
                "avg_speed": {"$avg": "$path_segments.average_speed_kmh"},
                "max_speed": {"$max": "$path_segments.average_speed_kmh"},
                "total_duration": {"$sum": "$path_segments.duration_seconds"}
            }}
        ]

        result = list(time_tracking_collection.aggregate(pipeline))

        if result:
            stats = result[0]
            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "movement_analytics": {
                    "total_distance_km": round(stats["total_distance"] / 1000, 2),
                    "total_segments": stats["total_segments"],
                    "average_speed_kmh": round(stats["avg_speed"], 2),
                    "max_speed_kmh": round(stats["max_speed"], 2),
                    "total_movement_hours": round(stats["total_duration"] / 3600, 2)
                }
            }
        else:
            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "movement_analytics": {
                    "total_distance_km": 0,
                    "total_segments": 0,
                    "average_speed_kmh": 0,
                    "max_speed_kmh": 0,
                    "total_movement_hours": 0
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get path analytics: {str(e)}"
        )
@router.post("/api/v1/time-tracking/check-auto-clockout")
async def check_auto_clockout():
    """Check and auto clock-out users who exceed 8 hours"""
    auto_clock_out_users()
    return {"message": "Auto clock-out check completed"}


@router.get("/api/v1/my-progress")
async def get_my_progress(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current user's work progress and stats"""
    try:
        users_collection = db.users
        leads_collection = db.leads
        time_tracking_collection = db.time_tracking

        user_id = current_user["id"]
        username = current_user["username"]

        # Get user info
        user = users_collection.find_one({"_id": ObjectId(user_id)})

        # Today's stats
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        # Today's work hours
        today_sessions = list(time_tracking_collection.find({
            "user_id": user_id,
            "clock_in_time": {"$gte": today_start, "$lt": today_end}
        }))

        today_hours = 0
        current_session = None
        for session in today_sessions:
            if session.get("clock_out_time"):
                today_hours += session.get("total_hours", 0)
            elif session.get("is_active"):
                current_session = session
                elapsed = datetime.utcnow() - session["clock_in_time"]
                today_hours += elapsed.total_seconds() / 3600

        # Today's leads
        today_leads = leads_collection.count_documents({
            "created_by": username,
            "created_at": {"$gte": today_start, "$lt": today_end}
        })

        # This week's stats
        week_start = today_start - timedelta(days=today_start.weekday())

        week_leads = leads_collection.count_documents({
            "created_by": username,
            "created_at": {"$gte": week_start}
        })

        # Lead status breakdown
        lead_stats = {}
        for status in ["pending", "approved", "sold", "cancelled"]:
            lead_stats[status] = leads_collection.count_documents({
                "created_by": username,
                "lead_status": status
            })

        # Points and earnings
        total_points = user.get("points", 0)

        return {
            "user_info": {
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "username": username,
                "points": total_points
            },
            "today": {
                "hours_worked": round(today_hours, 2),
                "hours_remaining": max(0, round(8 - today_hours, 2)),
                "leads_created": today_leads,
                "is_clocked_in": current_session is not None,
                "clock_in_time": current_session["clock_in_time"].isoformat() if current_session else None
            },
            "this_week": {
                "total_leads": week_leads,
                "leads_per_day": round(week_leads / 7, 1)
            },
            "overall_stats": {
                "total_leads": sum(lead_stats.values()),
                "pending_leads": lead_stats["pending"],
                "approved_leads": lead_stats["approved"],
                "sold_leads": lead_stats["sold"],
                "approval_rate": round(
                    (lead_stats["approved"] + lead_stats["sold"]) / max(sum(lead_stats.values()), 1) * 100, 1)
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get progress: {str(e)}"
        )


@router.post("/api/v1/live-tracking/cleanup")
async def cleanup_live_tracking(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Clean up old live tracking entries"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can cleanup live tracking data"
            )

        live_tracking_collection = db.live_tracking

        # Remove entries older than 24 hours
        yesterday = datetime.utcnow() - timedelta(hours=24)

        result = live_tracking_collection.delete_many({
            "last_update": {"$lt": yesterday},
            "is_active": False
        })

        return {
            "message": "Live tracking cleanup completed",
            "deleted_entries": result.deleted_count
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cleanup live tracking: {str(e)}"
        )


# ==================== POINT STORE API ENDPOINTS ====================

# 1. REWARD MANAGEMENT (Admin only)


