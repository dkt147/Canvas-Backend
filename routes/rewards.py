"""
Rewards endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Rewards'])

@router.post("/api/v1/rewards")
async def create_reward(
        reward_data: RewardCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new reward (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can create rewards"
            )

        rewards_collection = db.rewards

        # Generate reward ID
        org_id = current_user.get("organization_id", "global")
        reward_id = generate_reward_id(org_id)

        # Handle image upload
        uploaded_image_id = None
        final_image_url = reward_data.image_url

        if reward_data.image_base64:
            uploaded_image_id = save_reward_image(reward_data.image_base64, reward_id)
            if uploaded_image_id:
                final_image_url = f"/api/v1/rewards/images/{uploaded_image_id}"

        # Create reward document
        reward_doc = {
            "reward_id": reward_id,
            "name": reward_data.name,
            "description": reward_data.description,
            "category": reward_data.category.value,
            "points_required": reward_data.points_required,
            "stock_quantity": reward_data.stock_quantity,
            "image_url": final_image_url,
            "uploaded_image_id": uploaded_image_id,
            "is_featured": reward_data.is_featured,
            "terms_conditions": reward_data.terms_conditions,
            "estimated_delivery_days": reward_data.estimated_delivery_days,
            "organization_id": org_id,
            "status": RewardStatus.AVAILABLE.value,
            "is_active": reward_data.is_active,
            "created_by": current_user["username"],
            "created_at": datetime.utcnow()
        }

        result = rewards_collection.insert_one(reward_doc)

        return {
            "message": "Reward created successfully",
            "reward_id": reward_id,
            "database_id": str(result.inserted_id),
            "image_uploaded": bool(uploaded_image_id)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create reward: {str(e)}"
        )


@router.get("/api/v1/rewards")
async def list_rewards(
        category: Optional[str] = None,
        featured_only: bool = False,
        page: int = 1,
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List available rewards"""
    try:
        rewards_collection = db.rewards

        # Build filter
        filter_query = {"is_active": True}

        # Organization filter - show global rewards and organization-specific ones
        org_conditions = [{"organization_id": None}]  # Global rewards
        if current_user.get("organization_id"):
            org_conditions.append({"organization_id": current_user["organization_id"]})
        filter_query["$or"] = org_conditions

        if category:
            filter_query["category"] = category

        if featured_only:
            filter_query["is_featured"] = True

        # Only show available rewards to regular users
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            filter_query["status"] = RewardStatus.AVAILABLE.value

        # Count total
        total_count = rewards_collection.count_documents(filter_query)

        # Pagination
        skip = (page - 1) * limit

        rewards = []
        user_points = check_user_points(current_user["id"])

        for reward in rewards_collection.find(filter_query).skip(skip).limit(limit).sort("points_required", 1):
            # Get image data if available
            final_image_url = reward.get("image_url")
            if reward.get("uploaded_image_id"):
                final_image_url = f"/api/v1/rewards/images/{reward['uploaded_image_id']}"

            reward_info = {
                "id": str(reward["_id"]),
                "reward_id": reward["reward_id"],
                "name": reward["name"],
                "description": reward["description"],
                "category": reward["category"],
                "points_required": reward["points_required"],
                "stock_quantity": reward.get("stock_quantity"),
                "image_url": final_image_url,
                "is_featured": reward["is_featured"],
                "terms_conditions": reward.get("terms_conditions"),
                "estimated_delivery_days": reward.get("estimated_delivery_days", 7),
                "status": reward["status"],
                "user_can_afford": user_points >= reward["points_required"],
                "points_needed": max(0, reward["points_required"] - user_points),
                "is_available": (
                        reward["status"] == "available" and
                        (reward.get("stock_quantity") is None or reward.get("stock_quantity", 0) > 0)
                )
            }

            rewards.append(reward_info)

        return {
            "rewards": rewards,
            "user_points": user_points,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "categories": [cat.value for cat in RewardCategory]
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list rewards: {str(e)}"
        )
@router.get("/api/v1/rewards/categories")
async def get_reward_categories():  # REMOVED: current_user parameter
    """Get all reward categories (No auth required)"""
    return {
        "categories": [
            {"value": "electronics", "label": "Electronics", "icon": "📱"},
            {"value": "entertainment", "label": "Entertainment", "icon": "🎮"},
            {"value": "gift_cards", "label": "Gift Cards", "icon": "🎁"},
            {"value": "cash_rewards", "label": "Cash Rewards", "icon": "💰"},
            {"value": "experiences", "label": "Experiences", "icon": "🎪"},
            {"value": "merchandise", "label": "Merchandise", "icon": "👕"}
        ]
    }

@router.get("/api/v1/rewards/{reward_id}")
async def get_reward_details(
        reward_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get detailed reward information"""
    try:
        rewards_collection = db.rewards

        # Find reward
        reward = rewards_collection.find_one({"reward_id": reward_id})
        if not reward:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Reward not found"
            )

        user_points = check_user_points(current_user["id"])

        # Get full image data if available
        image_data = None
        if reward.get("uploaded_image_id"):
            reward_images_collection = db.reward_images
            img_doc = reward_images_collection.find_one({"image_id": reward["uploaded_image_id"]})
            if img_doc:
                image_data = f"data:image/jpeg;base64,{img_doc['image_data']}"

        return {
            "reward_id": reward["reward_id"],
            "name": reward["name"],
            "description": reward["description"],
            "category": reward["category"],
            "points_required": reward["points_required"],
            "stock_quantity": reward.get("stock_quantity"),
            "image_url": reward.get("image_url"),
            "image_data": image_data,
            "is_featured": reward["is_featured"],
            "terms_conditions": reward.get("terms_conditions"),
            "estimated_delivery_days": reward.get("estimated_delivery_days", 7),
            "status": reward["status"],
            "user_points": user_points,
            "user_can_afford": user_points >= reward["points_required"],
            "points_needed": max(0, reward["points_required"] - user_points),
            "is_available": (
                    reward["status"] == "available" and
                    (reward.get("stock_quantity") is None or reward.get("stock_quantity", 0) > 0)
            )
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get reward details: {str(e)}"
        )


# 2. REDEMPTION SYSTEM

@router.post("/api/v1/rewards/{reward_id}/redeem")
async def redeem_reward(
        reward_id: str,
        redemption_data: RedemptionRequest,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Redeem a reward with points"""
    try:
        rewards_collection = db.rewards
        redemptions_collection = db.redemptions

        # Find reward
        reward = rewards_collection.find_one({"reward_id": reward_id})
        if not reward:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Reward not found"
            )

        # Check if reward is available
        if reward["status"] != "available" or not reward["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reward is not available for redemption"
            )

        # Check stock
        if reward.get("stock_quantity") is not None and reward.get("stock_quantity", 0) <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reward is out of stock"
            )

        # Check user points
        user_points = check_user_points(current_user["id"])
        if user_points < reward["points_required"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient points. You have {user_points}, need {reward['points_required']}"
            )

        # Generate redemption ID
        redemption_id = generate_redemption_id(current_user.get("organization_id", "global"))

        # Create redemption record
        redemption_doc = {
            "redemption_id": redemption_id,
            "user_id": current_user["id"],
            "username": current_user["username"],
            "reward_id": reward_id,
            "reward_name": reward["name"],
            "points_used": reward["points_required"],
            "shipping_address": redemption_data.shipping_address,
            "contact_phone": redemption_data.contact_phone,
            "special_instructions": redemption_data.special_instructions,
            "status": RedemptionStatus.PENDING.value,
            "organization_id": current_user.get("organization_id"),
            "requested_at": datetime.utcnow(),
            "estimated_delivery": datetime.utcnow() + timedelta(days=reward.get("estimated_delivery_days", 7))
        }

        # Deduct points from user
        points_deducted = deduct_user_points(
            current_user["id"],
            reward["points_required"],
            f"Redeemed reward: {reward['name']} (ID: {redemption_id})"
        )

        if not points_deducted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to deduct points"
            )

        # Save redemption
        redemptions_collection.insert_one(redemption_doc)

        # Update stock if limited
        if reward.get("stock_quantity") is not None:
            rewards_collection.update_one(
                {"reward_id": reward_id},
                {"$inc": {"stock_quantity": -1}}
            )

            # Mark as out of stock if quantity reaches 0
            updated_reward = rewards_collection.find_one({"reward_id": reward_id})
            if updated_reward.get("stock_quantity", 0) <= 0:
                rewards_collection.update_one(
                    {"reward_id": reward_id},
                    {"$set": {"status": RewardStatus.OUT_OF_STOCK.value}}
                )

        return {
            "message": "Reward redeemed successfully!",
            "redemption_id": redemption_id,
            "reward_name": reward["name"],
            "points_used": reward["points_required"],
            "remaining_points": user_points - reward["points_required"],
            "estimated_delivery": redemption_doc["estimated_delivery"].isoformat(),
            "status": RedemptionStatus.PENDING.value
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to redeem reward: {str(e)}"
        )


@router.get("/api/v1/my-redemptions")
async def get_my_redemptions(
        page: int = 1,
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get user's redemption history"""
    try:
        redemptions_collection = db.redemptions

        # Build filter
        filter_query = {"user_id": current_user["id"]}

        # Count total
        total_count = redemptions_collection.count_documents(filter_query)

        # Pagination
        skip = (page - 1) * limit

        redemptions = []
        for redemption in redemptions_collection.find(filter_query).skip(skip).limit(limit).sort("requested_at", -1):
            redemptions.append({
                "redemption_id": redemption["redemption_id"],
                "reward_name": redemption["reward_name"],
                "points_used": redemption["points_used"],
                "status": redemption["status"],
                "requested_at": redemption["requested_at"].isoformat(),
                "estimated_delivery": redemption.get("estimated_delivery").isoformat() if redemption.get(
                    "estimated_delivery") else None,
                "tracking_number": redemption.get("tracking_number"),
                "shipping_address": redemption["shipping_address"]
            })

        return {
            "redemptions": redemptions,
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
            detail=f"Failed to get redemptions: {str(e)}"
        )


# 3. ADMIN REDEMPTION MANAGEMENT

@router.get("/api/v1/admin/redemptions")
async def list_all_redemptions(
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List all redemptions (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can view all redemptions"
            )

        redemptions_collection = db.redemptions
        users_collection = db.users

        # Build filter
        filter_query = {}

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]

        if status:
            filter_query["status"] = status

        # Count total
        total_count = redemptions_collection.count_documents(filter_query)

        # Pagination
        skip = (page - 1) * limit

        redemptions = []
        for redemption in redemptions_collection.find(filter_query).skip(skip).limit(limit).sort("requested_at", -1):
            # Get user info
            user = users_collection.find_one({"_id": ObjectId(redemption["user_id"])})
            user_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() if user else redemption[
                "username"]

            redemptions.append({
                "id": str(redemption["_id"]),
                "redemption_id": redemption["redemption_id"],
                "user_id": redemption["user_id"],
                "username": redemption["username"],
                "user_name": user_name,
                "reward_name": redemption["reward_name"],
                "points_used": redemption["points_used"],
                "status": redemption["status"],
                "shipping_address": redemption["shipping_address"],
                "contact_phone": redemption["contact_phone"],
                "special_instructions": redemption.get("special_instructions"),
                "requested_at": redemption["requested_at"].isoformat(),
                "estimated_delivery": redemption.get("estimated_delivery").isoformat() if redemption.get(
                    "estimated_delivery") else None,
                "tracking_number": redemption.get("tracking_number"),
                "admin_notes": redemption.get("admin_notes")
            })

        return {
            "redemptions": redemptions,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "summary": {
                "pending": redemptions_collection.count_documents({**filter_query, "status": "pending"}),
                "approved": redemptions_collection.count_documents({**filter_query, "status": "approved"}),
                "shipped": redemptions_collection.count_documents({**filter_query, "status": "shipped"}),
                "delivered": redemptions_collection.count_documents({**filter_query, "status": "delivered"}),
                "cancelled": redemptions_collection.count_documents({**filter_query, "status": "cancelled"})
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list redemptions: {str(e)}"
        )


@router.put("/api/v1/admin/redemptions/{redemption_id}")
async def update_redemption_status(
        redemption_id: str,
        update_data: RedemptionUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update redemption status (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can update redemption status"
            )

        redemptions_collection = db.redemptions

        # Find redemption
        redemption = redemptions_collection.find_one({"redemption_id": redemption_id})
        if not redemption:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Redemption not found"
            )

        # Handle cancellation - refund points
        if update_data.status == RedemptionStatus.CANCELLED and redemption["status"] != "cancelled":
            refund_success = refund_user_points(
                redemption["user_id"],
                redemption["points_used"],
                f"Refund for cancelled redemption: {redemption['reward_name']} (ID: {redemption_id})",
                current_user["username"]
            )

            if not refund_success:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Failed to refund points"
                )

            # Restore stock if applicable
            rewards_collection = db.rewards
            reward = rewards_collection.find_one({"reward_id": redemption["reward_id"]})
            if reward and reward.get("stock_quantity") is not None:
                rewards_collection.update_one(
                    {"reward_id": redemption["reward_id"]},
                    {
                        "$inc": {"stock_quantity": 1},
                        "$set": {"status": RewardStatus.AVAILABLE.value}
                    }
                )

        # Build update data
        update_fields = {
            "status": update_data.status.value,
            "updated_by": current_user["username"],
            "updated_at": datetime.utcnow()
        }

        if update_data.admin_notes:
            update_fields["admin_notes"] = update_data.admin_notes

        if update_data.tracking_number:
            update_fields["tracking_number"] = update_data.tracking_number

        # Set delivery date if status is delivered
        if update_data.status == RedemptionStatus.DELIVERED:
            update_fields["delivered_at"] = datetime.utcnow()

        # Update redemption
        redemptions_collection.update_one(
            {"redemption_id": redemption_id},
            {"$set": update_fields}
        )

        return {
            "message": "Redemption status updated successfully",
            "redemption_id": redemption_id,
            "new_status": update_data.status.value,
            "points_refunded": redemption["points_used"] if update_data.status == RedemptionStatus.CANCELLED else 0
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update redemption: {str(e)}"
        )


# 4. POINT STORE ANALYTICS

@router.get("/api/v1/admin/point-store/analytics")
async def get_point_store_analytics(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get point store analytics (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can view point store analytics"
            )

        # Date range setup
        if start_date:
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
        else:
            start_dt = datetime.utcnow() - timedelta(days=30)

        if end_date:
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
        else:
            end_dt = datetime.utcnow()

        redemptions_collection = db.redemptions
        rewards_collection = db.rewards

        # Build filter
        filter_query = {
            "requested_at": {"$gte": start_dt, "$lte": end_dt}
        }

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]

        # Redemption statistics
        total_redemptions = redemptions_collection.count_documents(filter_query)

        # Points analytics
        pipeline = [
            {"$match": filter_query},
            {"$group": {
                "_id": None,
                "total_points_redeemed": {"$sum": "$points_used"},
                "avg_points_per_redemption": {"$avg": "$points_used"},
                "redemptions_by_status": {"$push": "$status"}
            }}
        ]

        result = list(redemptions_collection.aggregate(pipeline))

        if result:
            stats = result[0]

            # Count by status
            status_counts = {}
            for status in stats["redemptions_by_status"]:
                status_counts[status] = status_counts.get(status, 0) + 1

            # Most popular rewards
            popular_rewards_pipeline = [
                {"$match": filter_query},
                {"$group": {
                    "_id": "$reward_id",
                    "reward_name": {"$first": "$reward_name"},
                    "redemption_count": {"$sum": 1},
                    "total_points": {"$sum": "$points_used"}
                }},
                {"$sort": {"redemption_count": -1}},
                {"$limit": 10}
            ]

            popular_rewards = list(redemptions_collection.aggregate(popular_rewards_pipeline))

            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_redemptions": total_redemptions,
                    "total_points_redeemed": stats["total_points_redeemed"],
                    "avg_points_per_redemption": round(stats["avg_points_per_redemption"], 2)
                },
                "status_breakdown": status_counts,
                "popular_rewards": popular_rewards,
                "conversion_rate": {
                    "completed": status_counts.get("delivered", 0),
                    "completion_rate": round(
                        (status_counts.get("delivered", 0) / max(total_redemptions, 1)) * 100, 2
                    )
                }
            }
        else:
            return {
                "period": {
                    "start_date": start_dt.isoformat(),
                    "end_date": end_dt.isoformat()
                },
                "summary": {
                    "total_redemptions": 0,
                    "total_points_redeemed": 0,
                    "avg_points_per_redemption": 0
                },
                "status_breakdown": {},
                "popular_rewards": [],
                "conversion_rate": {
                    "completed": 0,
                    "completion_rate": 0
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get analytics: {str(e)}"
        )


# 5. USER POINT INFORMATION

@router.get("/api/v1/my-points")
async def get_my_points(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get current user's point information and tier status"""
    try:
        users_collection = db.users
        redemptions_collection = db.redemptions

        # Get user data
        user = users_collection.find_one({"_id": ObjectId(current_user["id"])})
        current_points = user.get("points", 0)
        points_history = user.get("points_history", [])

        # Calculate tier status
        def get_tier_info(points):
            if points >= 950:
                return {"tier": "Elite", "next_tier": None, "points_to_next": 0}
            elif points >= 800:
                return {"tier": "Premium", "next_tier": "Elite", "points_to_next": 950 - points}
            elif points >= 700:
                return {"tier": "Gold", "next_tier": "Premium", "points_to_next": 800 - points}
            else:
                return {"tier": "Standard", "next_tier": "Gold", "points_to_next": 700 - points}

        tier_info = get_tier_info(current_points)

        # Recent point activity
        recent_activity = sorted(points_history[-10:], key=lambda x: x["timestamp"], reverse=True)
        for activity in recent_activity:
            activity["timestamp"] = activity["timestamp"].isoformat()

        # Pending redemptions
        pending_redemptions = list(redemptions_collection.find({
            "user_id": current_user["id"],
            "status": {"$in": ["pending", "approved", "shipped"]}
        }).sort("requested_at", -1))

        pending_points = sum(r["points_used"] for r in pending_redemptions)

        # Available points (current - pending)
        available_points = max(0, current_points - pending_points)

        return {
            "current_points": current_points,
            "available_points": available_points,
            "pending_points": pending_points,
            "tier_status": tier_info,
            "recent_activity": recent_activity,
            "pending_redemptions_count": len(pending_redemptions),
            "total_earned": sum(h["points"] for h in points_history if h["points"] > 0),
            "total_spent": sum(abs(h["points"]) for h in points_history if h["points"] < 0)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get point information: {str(e)}"
        )


# 6. REWARD IMAGES

@router.get("/api/v1/rewards/images/{image_id}")
async def get_reward_image(image_id: str):
    """Get reward image data"""
    try:
        reward_images_collection = db.reward_images
        image_doc = reward_images_collection.find_one({"image_id": image_id})

        if not image_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Image not found"
            )

        return {
            "image_id": image_id,
            "image_data": f"data:image/jpeg;base64,{image_doc['image_data']}",
            "uploaded_at": image_doc["uploaded_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get image: {str(e)}"
        )


# 7. REWARD CATEGORIES



# 8. LEADERBOARD WITH POINTS

@router.get("/api/v1/points-leaderboard")
async def get_points_leaderboard(
        period: str = "all_time",  # all_time, month, week
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get points leaderboard for organization"""
    try:
        users_collection = db.users

        # Build filter for organization
        filter_query = {"is_active": True}

        if current_user["role"] != "super_admin":
            filter_query["organization_id"] = current_user.get("organization_id")

        # Get users and sort by points
        users = list(users_collection.find(filter_query).sort("points", -1).limit(limit))

        leaderboard = []
        current_user_rank = None

        for i, user in enumerate(users, 1):
            user_entry = {
                "rank": i,
                "username": user["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"],
                "points": user.get("points", 0),
                "role": user["role"]
            }

            if str(user["_id"]) == current_user["id"]:
                current_user_rank = i
                user_entry["is_current_user"] = True

            leaderboard.append(user_entry)

        # If current user not in top list, add their position
        current_user_info = None
        if current_user_rank is None:
            current_user_doc = users_collection.find_one({"_id": ObjectId(current_user["id"])})
            if current_user_doc:
                # Find actual rank
                higher_users = users_collection.count_documents({
                    **filter_query,
                    "points": {"$gt": current_user_doc.get("points", 0)}
                })

                current_user_info = {
                    "rank": higher_users + 1,
                    "username": current_user_doc["username"],
                    "name": f"{current_user_doc.get('first_name', '')} {current_user_doc.get('last_name', '')}".strip() or
                            current_user_doc["username"],
                    "points": current_user_doc.get("points", 0),
                    "role": current_user_doc["role"],
                    "is_current_user": True
                }

        return {
            "leaderboard": leaderboard,
            "current_user_rank": current_user_rank,
            "current_user_info": current_user_info,
            "total_users": users_collection.count_documents(filter_query)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get leaderboard: {str(e)}"
        )

from fastapi import Query

@router.get("/api/v1/leads-leaderboard")
async def get_leads_leaderboard(
    period: str = Query("daily", enum=["daily", "weekly", "monthly"]),
    limit: int = 20,
    current_user: dict = Depends(get_current_user_from_token)
):
    """
    Get leads leaderboard for organization, ranked by number of leads created in a period (daily, weekly, monthly)
    """
    from datetime import datetime, timedelta

    users_collection = db.users
    leads_collection = db.leads

    # Calculate date range based on period
    now = datetime.utcnow()
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Build leads filter for org
    filter_query = {
        "created_at": {"$gte": start, "$lte": now},
        "is_active": True
    }
    if current_user["role"] != "super_admin":
        filter_query["organization_id"] = current_user.get("organization_id")

    # Aggregate lead counts by user
    pipeline = [
        {"$match": filter_query},
        {"$group": {
            "_id": "$created_by",
            "leads_count": {"$sum": 1}
        }},
        {"$sort": {"leads_count": -1}},
        {"$limit": limit}
    ]
    results = list(leads_collection.aggregate(pipeline))

    # Fetch user info for leaderboard
    leaderboard = []
    for i, row in enumerate(results, 1):
        user = users_collection.find_one({"username": row["_id"]})
        if user:
            leaderboard.append({
                "rank": i,
                "username": user["username"],
                "name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"],
                "leads": row["leads_count"],
                "role": user["role"]
            })

    return {
        "period": period,
        "leaderboard": leaderboard,
        "total_users": len(leaderboard)
    }

# ADD this new endpoint for managing participants:

@router.post("/api/v1/competitions/{competition_id}/participants")
async def update_competition_participants(
        competition_id: str,
        participant_usernames: List[str],
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update competition participants (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can update competition participants"
            )

        competitions_collection = db.competitions
        users_collection = db.users

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

        # Validate all participants
        valid_participants = []
        for username in participant_usernames:
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
            valid_participants.append(username)

        # Check minimum participants
        if len(valid_participants) < competition.get("min_participants", 2):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Must have at least {competition.get('min_participants', 2)} participants"
            )

        # Get previous participants
        old_participants = get_competition_participants(competition)
        old_usernames = [p["username"] for p in old_participants]

        # Update competition
        competitions_collection.update_one(
            {"competition_id": competition_id},
            {"$set": {
                "participant_selection_mode": "specific",
                "selected_participants": valid_participants,
                "updated_at": datetime.utcnow(),
                "updated_by": current_user["username"]
            }}
        )

        # Notify new participants
        new_participants = [u for u in valid_participants if u not in old_usernames]
        if new_participants:
            create_notification({
                "title": "🎯 Added to Competition!",
                "message": f"You've been added to the competition '{competition['title']}'",
                "type": NotificationType.COMPETITION_UPDATE.value,
                "recipient_usernames": new_participants,
                "priority": "high"
            })

        # Notify removed participants
        removed_participants = [u for u in old_usernames if u not in valid_participants]
        if removed_participants:
            create_notification({
                "title": "📢 Competition Update",
                "message": f"You've been removed from the competition '{competition['title']}'",
                "type": NotificationType.COMPETITION_UPDATE.value,
                "recipient_usernames": removed_participants,
                "priority": "normal"
            })

        return {
            "message": "Participants updated successfully",
            "competition_id": competition_id,
            "total_participants": len(valid_participants),
            "new_participants": len(new_participants),
            "removed_participants": len(removed_participants)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update participants: {str(e)}"
        )
# ADD this new endpoint to view competition participants:

@router.get("/api/v1/competitions/{competition_id}/participants")
async def get_competition_participants_list(
        competition_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get list of competition participants"""
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
            "competition_id": competition_id,
            "competition_title": competition["title"],
            "participant_selection_mode": competition.get("participant_selection_mode", "all"),
            "participants": participants,
            "total_participants": len(participants),
            "user_is_participant": user_is_participant,
            "min_participants": competition.get("min_participants", 2),
            "target_roles": competition.get("target_roles", [])
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get participants: {str(e)}"
        )


# ADD this new endpoint for competition analytics:

@router.get("/api/v1/competitions/analytics")
async def get_competition_analytics(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get competition analytics for organization"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can view competition analytics"
            )

        if not current_user.get("organization_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You must belong to an organization"
            )

        competitions_collection = db.competitions

        # Get all competitions for organization
        filter_query = {"organization_id": current_user["organization_id"]}

        total_competitions = competitions_collection.count_documents(filter_query)
        active_competitions = competitions_collection.count_documents({**filter_query, "status": "active"})
        completed_competitions = competitions_collection.count_documents({**filter_query, "status": "completed"})
        upcoming_competitions = competitions_collection.count_documents({**filter_query, "status": "upcoming"})

        # Get participant statistics
        all_competitions = list(competitions_collection.find(filter_query))

        total_participants = 0
        avg_participants = 0
        selection_mode_counts = {"all": 0, "roles": 0, "specific": 0}

        for comp in all_competitions:
            participants = get_competition_participants(comp)
            total_participants += len(participants)

            mode = comp.get("participant_selection_mode", "all")
            selection_mode_counts[mode] = selection_mode_counts.get(mode, 0) + 1

        if total_competitions > 0:
            avg_participants = total_participants / total_competitions

        # Get recent winners
        recent_completed = list(competitions_collection.find({
            **filter_query,
            "status": "completed"
        }).sort("completed_at", -1).limit(5))

        recent_winners = []
        for comp in recent_completed:
            if comp.get("winner"):
                recent_winners.append({
                    "competition_title": comp["title"],
                    "winner_username": comp["winner"].get("username"),
                    "winner_name": comp["winner"].get("name"),
                    "winner_score": comp["winner"].get("score"),
                    "completed_at": comp.get("completed_at").isoformat() if comp.get("completed_at") else None
                })

        return {
            "organization_id": current_user["organization_id"],
            "summary": {
                "total_competitions": total_competitions,
                "active_competitions": active_competitions,
                "completed_competitions": completed_competitions,
                "upcoming_competitions": upcoming_competitions
            },
            "participant_stats": {
                "total_unique_participants": total_participants,
                "avg_participants_per_competition": round(avg_participants, 2),
                "selection_mode_distribution": selection_mode_counts
            },
            "recent_winners": recent_winners
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get analytics: {str(e)}"
        )


# ==================== PERFORMANCE GOALS CONFIGURATION ====================

@router.get("/api/v1/admin/performance-goals")
async def get_performance_goals(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get performance goals configuration (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can view performance goals configuration"
            )

        org_id = current_user.get("organization_id") if current_user["role"] == "admin_manager" else None
        config = get_performance_goals_config(org_id)

        return {
            "config": {
                "daily_target_leads": config.get("daily_target_leads", 2),
                "bonus_target_leads": config.get("bonus_target_leads", 4),
                "bonus_amount": config.get("bonus_amount", 25.0),
                "daily_target_description": config.get("daily_target_description"),
                "bonus_description": config.get("bonus_description"),
                "is_active": config.get("is_active", True),
                "organization_id": config.get("organization_id")
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get performance goals: {str(e)}"
        )


@router.post("/api/v1/admin/performance-goals")
async def create_performance_goals(
        goals_config: PerformanceGoalsConfig,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create/Update performance goals configuration (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can configure performance goals"
            )

        performance_goals_collection = db.performance_goals

        # Determine organization_id
        if current_user["role"] == "admin_manager":
            org_id = current_user.get("organization_id")
            if not org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Admin manager must have an organization"
                )
        else:
            # Super admin can create global config
            org_id = None

        # Check if config already exists
        existing_config = performance_goals_collection.find_one({
            "organization_id": org_id
        })

        config_doc = {
            "organization_id": org_id,
            "daily_target_leads": goals_config.daily_target_leads,
            "bonus_target_leads": goals_config.bonus_target_leads,
            "bonus_amount": goals_config.bonus_amount,
            "daily_target_description": goals_config.daily_target_description,
            "bonus_description": goals_config.bonus_description,
            "is_active": goals_config.is_active,
            "updated_by": current_user["username"],
            "updated_at": datetime.utcnow()
        }

        if existing_config:
            # Update existing
            performance_goals_collection.update_one(
                {"_id": existing_config["_id"]},
                {"$set": config_doc}
            )
            message = "Performance goals updated successfully"
        else:
            # Create new
            config_doc["created_by"] = current_user["username"]
            config_doc["created_at"] = datetime.utcnow()
            performance_goals_collection.insert_one(config_doc)
            message = "Performance goals created successfully"

        return {
            "message": message,
            "config": config_doc,
            "applies_to": "All organizations" if org_id is None else f"Organization {org_id}"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to configure performance goals: {str(e)}"
        )


@router.put("/api/v1/admin/performance-goals")
async def update_performance_goals(
        goals_update: PerformanceGoalsUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update performance goals configuration (Admin only)"""
    try:
        if current_user["role"] not in ["super_admin", "admin_manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can update performance goals"
            )

        performance_goals_collection = db.performance_goals

        # Determine organization_id
        if current_user["role"] == "admin_manager":
            org_id = current_user.get("organization_id")
        else:
            org_id = None  # Super admin updates global config

        # Find existing config
        existing_config = performance_goals_collection.find_one({
            "organization_id": org_id
        })

        if not existing_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Performance goals configuration not found. Create one first."
            )

        # Build update document
        update_data = {}
        if goals_update.daily_target_leads is not None:
            update_data["daily_target_leads"] = goals_update.daily_target_leads
        if goals_update.bonus_target_leads is not None:
            update_data["bonus_target_leads"] = goals_update.bonus_target_leads
        if goals_update.bonus_amount is not None:
            update_data["bonus_amount"] = goals_update.bonus_amount
        if goals_update.daily_target_description is not None:
            update_data["daily_target_description"] = goals_update.daily_target_description
        if goals_update.bonus_description is not None:
            update_data["bonus_description"] = goals_update.bonus_description
        if goals_update.is_active is not None:
            update_data["is_active"] = goals_update.is_active

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )

        update_data["updated_by"] = current_user["username"]
        update_data["updated_at"] = datetime.utcnow()

        # Update
        performance_goals_collection.update_one(
            {"_id": existing_config["_id"]},
            {"$set": update_data}
        )

        return {
            "message": "Performance goals updated successfully",
            "updated_fields": list(update_data.keys()),
            "applies_to": "All organizations" if org_id is None else f"Organization {org_id}"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update performance goals: {str(e)}"
        )


@router.get("/api/v1/performance-goals")
async def get_my_performance_goals(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get performance goals for current user (All roles)"""
    try:
        # Get config for user's organization
        org_id = current_user.get("organization_id")
        config = get_performance_goals_config(org_id)

        # Get user's today stats
        users_collection = db.users
        leads_collection = db.leads

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        # Count today's leads
        today_leads = leads_collection.count_documents({
            "created_by": current_user["username"],
            "created_at": {"$gte": today_start, "$lt": today_end}
        })

        # Calculate progress
        daily_target = config.get("daily_target_leads", 2)
        bonus_target = config.get("bonus_target_leads", 4)

        daily_progress = min((today_leads / daily_target) * 100, 100) if daily_target > 0 else 0
        bonus_progress = min((today_leads / bonus_target) * 100, 100) if bonus_target > 0 else 0

        daily_needed = max(daily_target - today_leads, 0)
        bonus_needed = max(bonus_target - today_leads, 0)

        return {
            "goals": {
                "daily_target": {
                    "target": daily_target,
                    "description": config.get("daily_target_description"),
                    "current": today_leads,
                    "progress": round(daily_progress, 1),
                    "needed": daily_needed,
                    "achieved": today_leads >= daily_target
                },
                "bonus_goal": {
                    "target": bonus_target,
                    "bonus_amount": config.get("bonus_amount", 25.0),
                    "description": config.get("bonus_description"),
                    "current": today_leads,
                    "progress": round(bonus_progress, 1),
                    "needed": bonus_needed,
                    "achieved": today_leads >= bonus_target
                }
            },
            "today_stats": {
                "leads_created": today_leads,
                "date": today_start.isoformat()
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get performance goals: {str(e)}"
        )

# Add this new endpoint after your existing user endpoints

@router.get("/api/v1/organizations/{org_id}/users")
async def get_users_by_organization(
        org_id: str,
        role: Optional[str] = None,
        active_only: bool = True,
        page: int = 1,
        limit: int = 100,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get all users in a specific organization with filtering options"""
    try:
        users_collection = db.users
        orgs_collection = db.organizations

        # Permission check
        if current_user["role"] == "super_admin":
            # Super admin can view any organization
            pass
        elif current_user["role"] == "admin_manager":
            # Admin manager can only view their own organization
            if org_id != current_user.get("organization_id"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only view users in your own organization"
                )
        else:
            # Managers and canvassers cannot use this endpoint
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view organization users"
            )

        # Verify organization exists
        organization = orgs_collection.find_one({"org_id": org_id})
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Build filter query
        filter_query = {"organization_id": org_id}

        # Filter by active status
        if active_only:
            filter_query["is_active"] = True

        # Filter by role if specified
        if role:
            filter_query["role"] = role

        # Count total matching users
        total_count = users_collection.count_documents(filter_query)

        # Calculate pagination
        skip = (page - 1) * limit

        # Get users
        users = []
        for user in users_collection.find(filter_query).skip(skip).limit(limit).sort("created_at", -1):
            # Get manager name if applicable
            manager_name = None
            if user.get("manager_id"):
                manager = users_collection.find_one({"username": user["manager_id"]})
                if manager:
                    manager_name = f"{manager.get('first_name', '')} {manager.get('last_name', '')}".strip() or manager.get("username")

            users.append({
                "id": str(user["_id"]),
                "username": user["username"],
                "email": user.get("email"),
                "role": user["role"],
                "organization_id": user.get("organization_id"),
                "manager_id": user.get("manager_id"),
                "manager_name": manager_name,
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "phone": user.get("phone"),
                "is_active": user.get("is_active", True),
                "points": user.get("points", 0),
                "last_activity": user.get("last_activity").isoformat() if user.get("last_activity") else None,
                "created_at": user["created_at"].isoformat()
            })

        # Group users by role for summary
        role_distribution = {}
        all_org_users = list(users_collection.find({"organization_id": org_id, "is_active": True}))
        for user in all_org_users:
            role = user["role"]
            role_distribution[role] = role_distribution.get(role, 0) + 1

        # Calculate statistics
        total_active = users_collection.count_documents({
            "organization_id": org_id,
            "is_active": True
        })
        total_inactive = users_collection.count_documents({
            "organization_id": org_id,
            "is_active": False
        })

        return {
            "organization": {
                "org_id": organization["org_id"],
                "name": organization["name"],
                "plan": organization.get("plan", "basic"),
                "max_users": organization.get("max_users", 0)
            },
            "users": users,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "statistics": {
                "total_active": total_active,
                "total_inactive": total_inactive,
                "total_users": total_active + total_inactive,
                "role_distribution": role_distribution,
                "available_slots": max(0, organization.get("max_users", 0) - total_active)
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get organization users: {str(e)}"
        )


@router.get("/api/v1/organizations/{org_id}/users/summary")
async def get_organization_users_summary(
        org_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get summary statistics of users in organization"""
    try:
        users_collection = db.users
        orgs_collection = db.organizations
        leads_collection = db.leads

        # Permission check (same as above)
        if current_user["role"] == "super_admin":
            pass
        elif current_user["role"] == "admin_manager":
            if org_id != current_user.get("organization_id"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only view your own organization"
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view organization summary"
            )

        # Verify organization exists
        organization = orgs_collection.find_one({"org_id": org_id})
        if not organization:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found"
            )

        # Get all users in organization
        all_users = list(users_collection.find({"organization_id": org_id}))

        # Calculate statistics
        active_users = [u for u in all_users if u.get("is_active", True)]
        inactive_users = [u for u in all_users if not u.get("is_active", True)]

        # Group by role
        by_role = {}
        for user in active_users:
            role = user["role"]
            if role not in by_role:
                by_role[role] = {
                    "count": 0,
                    "total_points": 0,
                    "usernames": []
                }
            by_role[role]["count"] += 1
            by_role[role]["total_points"] += user.get("points", 0)
            by_role[role]["usernames"].append(user["username"])

        # Calculate average points
        total_points = sum(u.get("points", 0) for u in active_users)
        avg_points = round(total_points / len(active_users), 2) if active_users else 0

        # Get lead statistics
        org_leads = leads_collection.count_documents({
            "organization_id": org_id,
            "is_active": True
        })

        # Get recent activity (last 7 days)
        week_ago = datetime.utcnow() - timedelta(days=7)
        recent_active = users_collection.count_documents({
            "organization_id": org_id,
            "is_active": True,
            "last_activity": {"$gte": week_ago}
        })

        return {
            "organization": {
                "org_id": organization["org_id"],
                "name": organization["name"],
                "plan": organization.get("plan", "basic"),
                "max_users": organization.get("max_users", 0)
            },
            "user_statistics": {
                "total_users": len(all_users),
                "active_users": len(active_users),
                "inactive_users": len(inactive_users),
                "available_slots": max(0, organization.get("max_users", 0) - len(active_users)),
                "recent_active_users": recent_active
            },
            "points_statistics": {
                "total_points": total_points,
                "average_points": avg_points
            },
            "role_distribution": by_role,
            "lead_statistics": {
                "total_leads": org_leads
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get organization summary: {str(e)}"
        )


# Add this new endpoint after your existing endpoints

@router.get("/api/v1/users/{user_id}/performance")
async def get_user_performance(
        user_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get complete performance data for a specific user"""
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

        # Check if current user has permission to view this user's performance
        if not check_user_access(current_user, target_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view this user's performance"
            )

        username = target_user["username"]

        # Get today's date range
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        # Get this week's date range (last 7 days)
        week_start = today_start - timedelta(days=7)

        # ==================== TODAY'S PERFORMANCE ====================

        # Get today's leads
        today_leads = list(leads_collection.find({
            "created_by": username,
            "created_at": {"$gte": today_start, "$lt": today_end},
            "is_active": True
        }))

        today_qualified = [l for l in today_leads if l.get("lead_status") in ["approved", "sold", "superstar"]]
        today_appointments = [l for l in today_leads if l.get("preferred_appointment_time")]

        today_bonus_earned = 25 if len(today_leads) >= 4 else 0

        # ==================== WEEK'S PERFORMANCE ====================

        # Get week's leads
        week_leads = list(leads_collection.find({
            "created_by": username,
            "created_at": {"$gte": week_start, "$lt": today_end},
            "is_active": True
        }))

        week_qualified = [l for l in week_leads if l.get("lead_status") in ["approved", "sold", "superstar"]]
        week_appointments = [l for l in week_leads if l.get("preferred_appointment_time")]

        week_qualified_rate = round((len(week_qualified) / len(week_leads)) * 100, 1) if week_leads else 0
        week_conversion_rate = round((len(week_appointments) / len(week_leads)) * 100, 1) if week_leads else 0
        week_avg_per_day = round(len(week_leads) / 7, 1)
        week_bonus_earned = (len(week_leads) // 4) * 25

        # ==================== OVERALL STATISTICS ====================

        # Get all leads
        all_leads = list(leads_collection.find({
            "created_by": username,
            "is_active": True
        }))

        total_leads = len(all_leads)
        pending_leads = len([l for l in all_leads if l.get("lead_status") == "pending"])
        approved_leads = len([l for l in all_leads if l.get("lead_status") in ["approved", "superstar"]])
        sold_leads = len([l for l in all_leads if l.get("lead_status") == "sold"])
        cancelled_leads = len([l for l in all_leads if l.get("lead_status") == "cancelled"])

        approval_rate = round((approved_leads + sold_leads) / total_leads * 100, 1) if total_leads > 0 else 0

        # ==================== PERFORMANCE GOALS ====================

        daily_goal_progress = min((len(today_leads) / 2) * 100, 100) if len(today_leads) > 0 else 0
        daily_goal_remaining = max(2 - len(today_leads), 0)

        bonus_goal_progress = min((len(today_leads) / 4) * 100, 100) if len(today_leads) > 0 else 0
        bonus_goal_remaining = max(4 - len(today_leads), 0)

        # ==================== DAILY BREAKDOWN (LAST 7 DAYS) ====================

        daily_breakdown = []
        for i in range(6, -1, -1):
            day_date = today_start - timedelta(days=i)
            day_end = day_date + timedelta(days=1)

            day_leads = [l for l in week_leads
                         if day_date <= l["created_at"] < day_end]

            day_qualified = [l for l in day_leads
                             if l.get("lead_status") in ["approved", "sold", "superstar"]]

            day_appointments = [l for l in day_leads
                                if l.get("preferred_appointment_time")]

            day_bonus = 25 if len(day_leads) >= 4 else 0

            daily_breakdown.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_date.strftime("%a, %b %d"),
                "leads": len(day_leads),
                "qualified": len(day_qualified),
                "appointments": len(day_appointments),
                "bonus": day_bonus
            })

        # ==================== USER INFO ====================

        user_info = {
            "user_id": str(target_user["_id"]),
            "username": target_user["username"],
            "name": f"{target_user.get('first_name', '')} {target_user.get('last_name', '')}".strip() or target_user[
                "username"],
            "role": target_user["role"],
            "points": target_user.get("points", 0),
            "organization_name": get_organization_name(target_user.get("organization_id")),
            "is_active": target_user.get("is_active", True)
        }

        return {
            "user": user_info,
            "today": {
                "leads_today": len(today_leads),
                "qualified_leads": len(today_qualified),
                "appointments_set": len(today_appointments),
                "bonus_earned": today_bonus_earned,
                "goals": {
                    "daily_target": {
                        "current": len(today_leads),
                        "goal": 2,
                        "progress": daily_goal_progress,
                        "remaining": daily_goal_remaining
                    },
                    "bonus_goal": {
                        "current": len(today_leads),
                        "goal": 4,
                        "progress": bonus_goal_progress,
                        "remaining": bonus_goal_remaining,
                        "earned": len(today_leads) >= 4
                    }
                }
            },
            "week": {
                "total_leads": len(week_leads),
                "avg_per_day": week_avg_per_day,
                "qualified_leads": len(week_qualified),
                "qualified_rate": f"{week_qualified_rate}%",
                "appointments": len(week_appointments),
                "conversion_rate": f"{week_conversion_rate}%",
                "total_bonus": week_bonus_earned
            },
            "overall": {
                "total_leads": total_leads,
                "pending_leads": pending_leads,
                "approved_leads": approved_leads,
                "sold_leads": sold_leads,
                "cancelled_leads": cancelled_leads,
                "approval_rate": f"{approval_rate}%"
            },
            "daily_breakdown": daily_breakdown
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user performance: {str(e)}"
        )


@router.get("/api/v1/users/username/{username}/performance")
async def get_user_performance_by_username(
        username: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get user performance by username instead of user_id"""
    try:
        users_collection = db.users

        # Find user by username
        target_user = users_collection.find_one({"username": username})
        if not target_user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{username}' not found"
            )

        # Call the main performance endpoint
        return await get_user_performance(str(target_user["_id"]), current_user)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get user performance: {str(e)}"
        )

