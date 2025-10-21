
from datetime import datetime, timedelta
from typing import List, Optional
import uuid
import base64
import math
from bson import ObjectId
from app.core.database import db
from app.models.enums import *

# ==================== HELPER FUNCTIONS ====================

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def check_permission(current_user: dict, required_roles: List[str]):
    """Check if current user has required role"""
    if current_user.get("role") not in required_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required roles: {required_roles}"
        )


# ==================== HELPER FUNCTIONS ====================


def generate_reward_id(organization_id: str) -> str:
    """Generate unique reward ID"""
    rewards_collection = db.rewards

    if not organization_id:
        organization_id = "global"

    # Extract organization number
    org_number = organization_id.split('_')[-1] if '_' in organization_id else "001"

    # Keep trying until we find a unique ID
    counter = 1
    while True:
        candidate_id = f"REWARD_{org_number}_{str(counter).zfill(4)}"

        # Check if this ID already exists
        existing = rewards_collection.find_one({"reward_id": candidate_id})
        if not existing:
            return candidate_id

        counter += 1

        # Safety check to prevent infinite loop
        if counter > 9999:
            # Fall back to timestamp-based ID
            import time
            timestamp = str(int(time.time()))[-6:]  # Last 6 digits of timestamp
            return f"REWARD_{org_number}_{timestamp}"


# ==================== FIX 3: OPTIONAL DATABASE CLEANUP ====================
# If you want to clean up duplicate rewards, add this function and call it once:
def get_performance_goals_config(organization_id: str = None) -> dict:
    """Get performance goals configuration for organization or default"""
    try:
        performance_goals_collection = db.performance_goals

        # Try to get organization-specific config
        if organization_id:
            config = performance_goals_collection.find_one({
                "organization_id": organization_id,
                "is_active": True
            })
            if config:
                return config

        # Fall back to default/global config
        default_config = performance_goals_collection.find_one({
            "organization_id": None,
            "is_active": True
        })

        if default_config:
            return default_config

        # Return hardcoded defaults if nothing in database
        return {
            "daily_target_leads": 2,
            "bonus_target_leads": 4,
            "bonus_amount": 25.0,
            "daily_target_description": "Achieve 2+ leads per day to maintain good standing",
            "bonus_description": "Get 4+ leads in a day to earn $25 bonus",
            "is_active": True
        }

    except Exception as e:
        print(f"Error getting performance goals config: {e}")
        # Return defaults
        return {
            "daily_target_leads": 2,
            "bonus_target_leads": 4,
            "bonus_amount": 25.0,
            "daily_target_description": "Achieve 2+ leads per day to maintain good standing",
            "bonus_description": "Get 4+ leads in a day to earn $25 bonus",
            "is_active": True
        }
def cleanup_duplicate_rewards():
    """Clean up duplicate reward IDs (run once)"""
    try:
        rewards_collection = db.rewards

        # Find all rewards with the same reward_id
        pipeline = [
            {"$group": {
                "_id": "$reward_id",
                "count": {"$sum": 1},
                "docs": {"$push": "$_id"}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]

        duplicates = list(rewards_collection.aggregate(pipeline))

        for duplicate in duplicates:
            # Keep the first document, remove the rest
            docs_to_remove = duplicate["docs"][1:]  # Keep first, remove others

            for doc_id in docs_to_remove:
                rewards_collection.delete_one({"_id": doc_id})
                print(f"Removed duplicate reward with ID: {doc_id}")

        print(f"Cleaned up {len(duplicates)} duplicate reward groups")

    except Exception as e:
        print(f"Error during cleanup: {e}")


def generate_redemption_id(organization_id: str) -> str:
    """Generate unique redemption ID"""
    redemptions_collection = db.redemptions

    if not organization_id:
        organization_id = "global"

    count = redemptions_collection.count_documents({"organization_id": organization_id})
    org_number = organization_id.split('_')[-1] if '_' in organization_id else "001"
    return f"REDEEM_{org_number}_{str(count + 1).zfill(4)}"


def save_reward_image(base64_data: str, reward_id: str) -> str:
    """Save reward image to database"""
    try:
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]

        image_data = base64.b64decode(base64_data)
        image_id = f"reward_img_{str(uuid.uuid4())[:12]}"

        image_doc = {
            "image_id": image_id,
            "reward_id": reward_id,
            "image_data": base64_data,
            "file_size": len(image_data),
            "uploaded_at": datetime.utcnow()
        }

        reward_images_collection = db.reward_images
        reward_images_collection.insert_one(image_doc)
        return image_id

    except Exception as e:
        print(f"Error saving reward image: {e}")
        return None


def check_user_points(user_id: str) -> int:
    """Get current user points"""
    users_collection = db.users
    user = users_collection.find_one({"_id": ObjectId(user_id)})
    return user.get("points", 0) if user else 0


def deduct_user_points(user_id: str, points: int, reason: str, admin_username: str = None):
    """Deduct points from user account"""
    users_collection = db.users

    # Get current points
    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        return False

    current_points = user.get("points", 0)
    if current_points < points:
        return False

    # Deduct points
    new_points = current_points - points

    points_history_entry = {
        "action": "deduct",
        "points": -points,
        "old_value": current_points,
        "new_value": new_points,
        "reason": reason,
        "deducted_by": admin_username or "system",
        "timestamp": datetime.utcnow()
    }

    users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {"points": new_points},
            "$push": {"points_history": points_history_entry}
        }
    )

    return True


def ensure_point_store_collections():
    """Ensure point store collections exist with proper structure"""
    try:
        # Create collections if they don't exist
        rewards_collection = db.rewards
        redemptions_collection = db.redemptions
        reward_images_collection = db.reward_images

        # Test insert and delete to create collections
        test_doc = {"test": True}

        result1 = rewards_collection.insert_one(test_doc)
        rewards_collection.delete_one({"_id": result1.inserted_id})

        result2 = redemptions_collection.insert_one(test_doc)
        redemptions_collection.delete_one({"_id": result2.inserted_id})

        result3 = reward_images_collection.insert_one(test_doc)
        reward_images_collection.delete_one({"_id": result3.inserted_id})

        print("✅ Point store collections initialized")

    except Exception as e:
        print(f"❌ Error initializing point store collections: {e}")

def refund_user_points(user_id: str, points: int, reason: str, admin_username: str):
    """Refund points to user account"""
    users_collection = db.users

    user = users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        return False

    current_points = user.get("points", 0)
    new_points = current_points + points

    points_history_entry = {
        "action": "refund",
        "points": points,
        "old_value": current_points,
        "new_value": new_points,
        "reason": reason,
        "refunded_by": admin_username,
        "timestamp": datetime.utcnow()
    }

    users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {"points": new_points},
            "$push": {"points_history": points_history_entry}
        }
    )

    return True


def create_performance_goals_indexes():
    """Create performance goals indexes"""
    try:
        performance_goals_collection = db.performance_goals
        performance_goals_collection.create_index("organization_id")
        performance_goals_collection.create_index("is_active")

        print("✅ Performance goals indexes created")
    except Exception as e:
        print(f"❌ Error creating performance goals indexes: {e}")
def create_notification_indexes():
    """Create notification indexes"""
    try:
        notifications_collection = db.notifications
        notifications_collection.create_index("notification_id", unique=True)
        notifications_collection.create_index("recipient_usernames")
        notifications_collection.create_index("is_read")
        notifications_collection.create_index("created_at")
        notifications_collection.create_index("expires_at")

        print("✅ Notification indexes created")
    except Exception as e:
        print(f"❌ Error creating notification indexes: {e}")

def create_reward_indexes():
    """Create indexes for rewards collection"""
    try:
        rewards_collection = db.rewards
        rewards_collection.create_index("reward_id", unique=True)
        rewards_collection.create_index("organization_id")
        rewards_collection.create_index("category")
        rewards_collection.create_index("points_required")
        rewards_collection.create_index("is_active")
        rewards_collection.create_index("is_featured")

        # Redemptions indexes
        redemptions_collection = db.redemptions
        redemptions_collection.create_index("redemption_id", unique=True)
        redemptions_collection.create_index("user_id")
        redemptions_collection.create_index("reward_id")
        redemptions_collection.create_index("status")
        redemptions_collection.create_index("organization_id")

        # Reward images indexes
        reward_images_collection = db.reward_images
        reward_images_collection.create_index("image_id", unique=True)
        reward_images_collection.create_index("reward_id")

        print("✅ Point store indexes created")
    except Exception as e:
        print(f"❌ Error creating point store indexes: {e}")


def create_sample_rewards():
    """Create sample rewards for testing"""
    try:
        rewards_collection = db.rewards

        sample_rewards = [
            {
                "reward_id": "REWARD_001_0001",
                "name": "50\" 4K Smart TV",
                "description": "Ultra HD Smart TV with streaming apps built-in",
                "category": "electronics",
                "points_required": 700,
                "stock_quantity": 5,
                "image_url": "/api/v1/rewards/images/tv_sample.jpg",
                "is_featured": True,
                "terms_conditions": "Delivery within 7-10 business days. Warranty included.",
                "estimated_delivery_days": 7,
                "organization_id": "org_001",
                "status": "available",
                "is_active": True,
                "created_at": datetime.utcnow(),
                "created_by": "system"
            },
            {
                "reward_id": "REWARD_001_0002",
                "name": "Apple iPad 10.2\"",
                "description": "Latest generation iPad with Retina display",
                "category": "electronics",
                "points_required": 700,
                "stock_quantity": 3,
                "is_featured": True,
                "terms_conditions": "Latest model. Includes charging cable.",
                "estimated_delivery_days": 5,
                "organization_id": "org_001",
                "status": "available",
                "is_active": True,
                "created_at": datetime.utcnow(),
                "created_by": "system"
            },
            {
                "reward_id": "REWARD_001_0003",
                "name": "SteelSeries Wireless Gaming Headphones",
                "description": "Premium wireless gaming headset with noise cancellation",
                "category": "electronics",
                "points_required": 700,
                "stock_quantity": 10,
                "is_featured": True,
                "estimated_delivery_days": 3,
                "organization_id": "org_001",
                "status": "available",
                "is_active": True,
                "created_at": datetime.utcnow(),
                "created_by": "system"
            },
            {
                "reward_id": "REWARD_001_0004",
                "name": "$100 Amazon Gift Card",
                "description": "Digital Amazon gift card - delivered via email",
                "category": "gift_cards",
                "points_required": 400,
                "stock_quantity": None,  # Unlimited
                "estimated_delivery_days": 1,
                "organization_id": "org_001",
                "status": "available",
                "is_active": True,
                "created_at": datetime.utcnow(),
                "created_by": "system"
            },
            {
                "reward_id": "REWARD_001_0005",
                "name": "Cash Reward - $200",
                "description": "Direct cash reward deposited to your account",
                "category": "cash_rewards",
                "points_required": 800,
                "stock_quantity": None,
                "estimated_delivery_days": 3,
                "organization_id": "org_001",
                "status": "available",
                "is_active": True,
                "created_at": datetime.utcnow(),
                "created_by": "system"
            }
        ]

        for reward in sample_rewards:
            existing = rewards_collection.find_one({"reward_id": reward["reward_id"]})
            if not existing:
                rewards_collection.insert_one(reward)
                print(f"✅ Created sample reward: {reward['name']}")

    except Exception as e:
        print(f"❌ Error creating sample rewards: {e}")


async def get_current_user_from_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current user info from JWT token"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        users_collection = db.users
        user = users_collection.find_one({"_id": ObjectId(user_id)})

        if user is None:
            raise HTTPException(status_code=401, detail="User not found")

        # Update last activity
        users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"last_activity": datetime.utcnow()}}
        )

        return {
            "id": str(user["_id"]),
            "username": user["username"],
            "role": user["role"],
            "organization_id": user.get("organization_id"),
            "email": user.get("email")
        }
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_organization_name(org_id: str) -> str:
    """Get organization name by ID"""
    if not org_id:
        return None

    orgs_collection = db.organizations
    org = orgs_collection.find_one({"org_id": org_id})
    return org.get("name") if org else None


def get_manager_name(manager_id: str) -> str:
    """Get manager name by ID"""
    if not manager_id:
        return None

    users_collection = db.users
    manager = users_collection.find_one({"username": manager_id})
    if manager:
        first_name = manager.get("first_name", "")
        last_name = manager.get("last_name", "")
        return f"{first_name} {last_name}".strip() or manager.get("username")
    return None


def generate_lead_id(organization_id: str) -> str:
    """Generate unique lead ID"""
    leads_collection = db.leads
    count = leads_collection.count_documents({"organization_id": organization_id})
    org_number = organization_id.split('_')[-1] if '_' in organization_id else "001"
    return f"LEAD_{org_number}_{str(count + 1).zfill(4)}"


def save_property_photo(base64_data: str, lead_id: str) -> str:
    """Save base64 image to database"""
    if not base64_data:
        return None

    try:
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]

        image_data = base64.b64decode(base64_data)

        photo_doc = {
            "lead_id": lead_id,
            "image_data": base64_data,
            "uploaded_at": datetime.utcnow(),
            "file_size": len(image_data)
        }

        photos_collection = db.lead_photos
        result = photos_collection.insert_one(photo_doc)
        return str(result.inserted_id)

    except Exception as e:
        print(f"Error saving photo: {e}")
        return None


def auto_clock_out_users():
    """Auto clock-out users who have been working for 8+ hours"""
    try:
        time_tracking_collection = db.time_tracking

        # Find sessions that have been active for 8+ hours
        eight_hours_ago = datetime.utcnow() - timedelta(hours=8)

        long_sessions = time_tracking_collection.find({
            "clock_out_time": None,
            "is_active": True,
            "clock_in_time": {"$lte": eight_hours_ago}
        })

        auto_clocked_count = 0
        for session in long_sessions:
            clock_out_time = datetime.utcnow()
            clock_in_time = session["clock_in_time"]

            # Calculate total hours (exactly 8 hours)
            total_hours = 8.0

            # Update session
            time_tracking_collection.update_one(
                {"_id": session["_id"]},
                {"$set": {
                    "clock_out_time": clock_out_time,
                    "total_hours": total_hours,
                    "is_active": False,
                    "auto_clocked_out": True,
                    "auto_clock_reason": "8 hour limit reached",
                    "updated_at": clock_out_time
                }}
            )
            auto_clocked_count += 1

        if auto_clocked_count > 0:
            print(f"Auto clocked out {auto_clocked_count} users")

    except Exception as e:
        print(f"Error in auto clock-out: {e}")
# ==================== ENHANCED HELPER FUNCTIONS ====================

def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points using Haversine formula (returns meters)"""
    R = 6371000  # Earth's radius in meters

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (math.sin(delta_lat / 2) * math.sin(delta_lat / 2) +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) * math.sin(delta_lon / 2))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def calculate_speed(distance_meters: float, time_seconds: float) -> float:
    """Calculate speed in km/h"""
    if time_seconds <= 0:
        return 0
    return (distance_meters / 1000) / (time_seconds / 3600)


def is_stationary(points: List[dict], threshold_meters: float = 50) -> bool:
    """Check if user has been stationary based on recent points"""
    if len(points) < 3:
        return False

    recent_points = points[-3:]
    max_distance = 0

    for i in range(len(recent_points)):
        for j in range(i + 1, len(recent_points)):
            distance = calculate_distance(
                recent_points[i]["latitude"], recent_points[i]["longitude"],
                recent_points[j]["latitude"], recent_points[j]["longitude"]
            )
            max_distance = max(max_distance, distance)

    return max_distance < threshold_meters


def detect_activity_type(current_point: dict, previous_points: List[dict]) -> str:
    """Detect activity type based on movement patterns"""
    if len(previous_points) < 2:
        return "unknown"

    # Check if stationary
    if is_stationary(previous_points + [current_point]):
        return "stationary"

    # Calculate recent speed
    if len(previous_points) >= 1:
        prev_point = previous_points[-1]
        distance = calculate_distance(
            prev_point["latitude"], prev_point["longitude"],
            current_point["latitude"], current_point["longitude"]
        )
        time_diff = (current_point["timestamp"] - prev_point["timestamp"]).total_seconds()
        if time_diff > 0:
            speed = calculate_speed(distance, time_diff)

            if speed < 2:  # Less than 2 km/h
                return "walking"
            elif speed < 15:  # 2-15 km/h
                return "cycling"
            else:  # More than 15 km/h
                return "driving"

    return "moving"


def create_path_segment(start_point: dict, end_point: dict) -> dict:
    """Create a path segment between two points"""
    distance = calculate_distance(
        start_point["latitude"], start_point["longitude"],
        end_point["latitude"], end_point["longitude"]
    )

    time_diff = (end_point["timestamp"] - start_point["timestamp"]).total_seconds()
    speed = calculate_speed(distance, time_diff) if time_diff > 0 else 0

    return {
        "start_point": start_point,
        "end_point": end_point,
        "distance_meters": distance,
        "duration_seconds": time_diff,
        "average_speed_kmh": speed,
        "created_at": datetime.utcnow()
    }


def check_lead_access(current_user: dict, lead: dict) -> bool:
    """Check if user has access to this lead"""
    if current_user["role"] == "super_admin":
        return True

    if current_user["role"] == "admin_manager":
        return lead.get("organization_id") == current_user["organization_id"]

    if current_user["role"] == "manager":
        if lead.get("organization_id") != current_user["organization_id"]:
            return False

        users_collection = db.users
        creator = users_collection.find_one({"username": lead.get("created_by")})
        if creator and creator.get("manager_id") == current_user["username"]:
            return True
        return False

    if current_user["role"] == "canvasser":
        return lead.get("created_by") == current_user["username"]

    return False


def check_user_access(current_user: dict, target_user: dict) -> bool:
    """Check if current user can access/modify target user"""
    if current_user["role"] == "super_admin":
        return True

    if current_user["role"] == "admin_manager":
        # Admin manager can access users in their organization
        return target_user.get("organization_id") == current_user["organization_id"]

    if current_user["role"] == "manager":
        # Manager can access themselves and their assigned canvassers
        if str(target_user["_id"]) == current_user["id"]:
            return True
        return (target_user.get("manager_id") == current_user["username"] and
                target_user.get("role") == "canvasser")

    if current_user["role"] == "canvasser":
        # Canvasser can only access themselves
        return str(target_user["_id"]) == current_user["id"]

    return False


def save_project_image(base64_data: str, project_id: str, caption: str = None) -> str:
    """Save base64 image to database and return image_id - FIXED VERSION"""
    try:
        # Remove data URL prefix if present
        if ',' in base64_data:
            base64_data = base64_data.split(',')[1]

        # Validate base64 data
        if not base64_data or len(base64_data) < 100:  # Too small to be a real image
            print(f"⚠️ Warning: Base64 data too small ({len(base64_data)} chars) - likely a placeholder")
            return None

        # Decode and validate image data
        try:
            image_data = base64.b64decode(base64_data)
        except Exception as e:
            print(f"❌ Failed to decode base64: {e}")
            return None

        # Check if decoded data is large enough (min 1KB for real images)
        if len(image_data) < 1024:  # Less than 1KB
            print(f"⚠️ Warning: Decoded image too small ({len(image_data)} bytes) - likely a placeholder")
            return None

        image_id = f"img_{str(uuid.uuid4())[:12]}"

        image_doc = {
            "image_id": image_id,
            "project_id": project_id,
            "image_data": base64_data,  # Store the cleaned base64 string
            "caption": caption,
            "file_size": len(image_data),
            "uploaded_at": datetime.utcnow()
        }

        images_collection = db.project_images
        images_collection.insert_one(image_doc)

        print(f"✅ Saved image {image_id}: {len(image_data)} bytes ({len(image_data) / 1024:.2f} KB)")
        return image_id

    except Exception as e:
        print(f"❌ Error saving project image: {e}")
        import traceback
        traceback.print_exc()
        return None
def generate_news_id(organization_id: str) -> str:
    """Generate unique news ID"""
    news_collection = db.newss

    if not organization_id:
        organization_id = "global"

    org_number = organization_id.split('_')[-1] if '_' in organization_id else "001"

    # Keep trying until we find a unique ID
    counter = 1
    while True:
        candidate_id = f"NEWS_{org_number}_{str(counter).zfill(4)}"

        # Check if this ID already exists
        existing = news_collection.find_one({"news_id": candidate_id})
        if not existing:
            return candidate_id

        counter += 1

        # Safety check to prevent infinite loop
        if counter > 9999:
            # Fall back to timestamp-based ID
            import time
            timestamp = str(int(time.time()))[-6:]  # Last 6 digits of timestamp
            return f"NEWS_{org_number}_{timestamp}"
def check_news_permission(current_user: dict, action: str = "view") -> bool:
    """Check if user can perform news actions"""
    if action == "view":
        return True  # Everyone can view news

    if action == "create":
        return current_user["role"] in ["super_admin", "admin_manager", "manager"]

    if action == "update":
        return current_user["role"] in ["super_admin", "admin_manager", "manager"]

    if action == "delete":
        return current_user["role"] in ["super_admin", "admin_manager", "manager"]

    if action == "pin":
        return current_user["role"] in ["super_admin", "admin_manager"]  # Admin only

    return False


# Step 3: Add these helper functions for checking limits (add after existing helper functions)

def check_organization_limits(organization_id: str, limit_type: str, count: int = 1) -> dict:
    """Check if organization can perform action based on their plan limits"""
    if not organization_id:
        return {"allowed": True, "message": "No organization limit"}

    orgs_collection = db.organizations
    org = orgs_collection.find_one({"org_id": organization_id})

    if not org:
        return {"allowed": False, "message": "Organization not found"}

    plan_limits = org.get("plan_limits", get_organization_limits("basic"))
    limit_value = plan_limits.get(limit_type, 0)

    # If limit is -1, it means unlimited
    if limit_value == -1:
        return {"allowed": True, "message": "Unlimited"}

    # Count current usage
    current_count = 0

    if limit_type == "max_projects":
        projects_collection = db.projects
        current_count = projects_collection.count_documents({
            "organization_id": organization_id,
            "is_active": True
        })
    elif limit_type == "max_users":
        users_collection = db.users
        current_count = users_collection.count_documents({
            "organization_id": organization_id,
            "is_active": True
        })

    if current_count + count > limit_value:
        return {
            "allowed": False,
            "message": f"Limit exceeded. Current: {current_count}, Limit: {limit_value}, Plan: {org.get('plan', 'basic')}"
        }

    return {
        "allowed": True,
        "message": f"Within limits. Current: {current_count}, Limit: {limit_value}"
    }


def check_project_image_limits(organization_id: str, current_images: int, new_images: int) -> dict:
    """Check project image limits"""
    if not organization_id:
        return {"allowed": True, "message": "No organization limit"}

    orgs_collection = db.organizations
    org = orgs_collection.find_one({"org_id": organization_id})

    if not org:
        return {"allowed": False, "message": "Organization not found"}

    plan_limits = org.get("plan_limits", get_organization_limits("basic"))
    limit_value = plan_limits.get("max_project_images", 5)

    # If limit is -1, it means unlimited
    if limit_value == -1:
        return {"allowed": True, "message": "Unlimited"}

    if current_images + new_images > limit_value:
        return {
            "allowed": False,
            "message": f"Project image limit exceeded. Current: {current_images}, Adding: {new_images}, Limit: {limit_value}"
        }

    return {
        "allowed": True,
        "message": f"Within project image limits. Total will be: {current_images + new_images}, Limit: {limit_value}"
    }
# Add this function after your existing helper functions
def get_organization_limits(plan: str) -> dict:
    """Get limits based on organization plan"""
    limits = {
        "basic": {
            "max_projects": 10,
            "max_project_images": 5,
            "max_news_images": 2,
            "max_users": 10,
            "features": ["basic_time_tracking", "basic_reporting"]
        },
        "professional": {
            "max_projects": 50,
            "max_project_images": 15,
            "max_news_images": 5,
            "max_users": 50,
            "features": ["advanced_time_tracking", "detailed_reporting", "location_tracking"]
        },
        "enterprise": {
            "max_projects": -1,  # unlimited
            "max_project_images": -1,  # unlimited
            "max_news_images": -1,  # unlimited
            "max_users": -1,  # unlimited
            "features": ["all_features", "custom_branding", "api_access"]
        }
    }
    return limits.get(plan, limits["basic"])

def calculate_expiration_date(hours: str) -> datetime:
    """Calculate expiration date based on hours"""
    return datetime.utcnow() + timedelta(hours=int(hours))


def is_news_expired(expiration_date: datetime) -> bool:
    """Check if news is expired"""
    return datetime.utcnow() > expiration_date

# 3. FIX THE GENERATE_PROJECT_ID FUNCTION TO HANDLE NONE VALUES
# Replace the existing generate_project_id function with this:

def generate_project_id(organization_id: str) -> str:
    """Generate unique project ID"""
    projects_collection = db.projects

    # Handle None or empty organization_id
    if not organization_id:
        organization_id = "default_org"

    count = projects_collection.count_documents({"organization_id": organization_id})
    org_number = organization_id.split('_')[-1] if '_' in organization_id else "001"
    return f"PROJ_{org_number}_{str(count + 1).zfill(4)}"


def check_project_permission(current_user: dict, action: str = "view") -> bool:
    """Check if user can perform project actions"""
    if action == "view":
        return True  # Everyone can view projects

    if action in ["create", "update", "delete"]:
        return current_user["role"] in ["super_admin", "admin_manager"]

    return False


def migrate_existing_time_sessions():
    """Add break fields to existing time tracking sessions"""
    try:
        time_tracking_collection = db.time_tracking

        # Update all existing sessions without break fields
        result = time_tracking_collection.update_many(
            {"breaks": {"$exists": False}},
            {
                "$set": {
                    "breaks": [],
                    "on_break": False,
                    "total_break_minutes": 0,
                    "work_hours": "$total_hours"  # Copy total_hours to work_hours for existing data
                }
            }
        )

        print(f"✅ Updated {result.modified_count} time tracking sessions with break fields")

        # Fix work_hours calculation for existing completed sessions
        completed_sessions = time_tracking_collection.find({
            "clock_out_time": {"$ne": None},
            "work_hours": {"$exists": False}
        })

        for session in completed_sessions:
            work_hours = session.get("total_hours", 0)  # For existing sessions, work_hours = total_hours
            time_tracking_collection.update_one(
                {"_id": session["_id"]},
                {"$set": {"work_hours": work_hours}}
            )

    except Exception as e:
        print(f"❌ Error migrating time sessions: {e}")
# ==================== ADD THESE API ENDPOINTS AT THE END OF YOUR EXISTING CODE ====================

# ==================== PROJECT PORTFOLIO MANAGEMENT ====================


