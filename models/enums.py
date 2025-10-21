"""
Enumerations for the application
"""
from enum import Enum

# ==================== ENUMS ====================
# Add this after your existing enums
from enum import Enum
from typing import Optional, List
import uuid

# ==================== POINT STORE ENUMS AND MODELS ====================
# Add these imports at the top of your file
from typing import Dict, List
from collections import defaultdict
import asyncio

# ==================== NOTIFICATION SYSTEM ====================
# ==================== PERFORMANCE GOALS MODELS ====================

class PerformanceGoalsConfig(BaseModel):
    daily_target_leads: int = 2
    bonus_target_leads: int = 4
    bonus_amount: float = 25.0
    daily_target_description: str = "Achieve 2+ leads per day to maintain good standing"
    bonus_description: str = "Get 4+ leads in a day to earn $25 bonus"
    is_active: bool = True

class PerformanceGoalsUpdate(BaseModel):
    daily_target_leads: Optional[int] = None
    bonus_target_leads: Optional[int] = None
    bonus_amount: Optional[float] = None
    daily_target_description: Optional[str] = None
    bonus_description: Optional[str] = None
    is_active: Optional[bool] = None
# Add this global dictionary to store active WebSocket connections (if using WebSockets)
active_connections: Dict[str, list] = defaultdict(list)

class NotificationType(str, Enum):
    NEW_LEAD = "new_lead"
    LEAD_APPROVED = "lead_approved"
    LEAD_REJECTED = "lead_rejected"
    COMPETITION_UPDATE = "competition_update"
    LEADERBOARD_CHANGE = "leaderboard_change"

class NotificationCreate(BaseModel):
    title: str
    message: str
    type: NotificationType
    recipient_usernames: List[str]
    data: Optional[dict] = None
class RewardCategory(str, Enum):
    ELECTRONICS = "electronics"
    ENTERTAINMENT = "entertainment"
    GIFT_CARDS = "gift_cards"
    CASH_REWARDS = "cash_rewards"
    EXPERIENCES = "experiences"
    MERCHANDISE = "merchandise"

class RewardStatus(str, Enum):
    AVAILABLE = "available"
    OUT_OF_STOCK = "out_of_stock"
    DISCONTINUED = "discontinued"

class RedemptionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class BreakType(str, Enum):
    LUNCH = "lunch"
    PERSONAL = "personal"
    SICK = "sick"
    EMERGENCY = "emergency"
    OTHER = "other"

class BreakStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class OrganizationPlan(str, Enum):
    BASIC = "basic"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class LeadStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SOLD = "sold"
    CANCELLED = "cancelled"
    SUPERSTAR = "superstar"
class PlanUpgradeRequest(BaseModel):
    new_plan: OrganizationPlan

class MaritalStatus(str, Enum):
    SINGLE = "single"
    MARRIED = "married"
    WIDOW = "widow"
    DIVORCED = "divorced"


class ProductType(str, Enum):
    KITCHEN_REMODELING = "kitchen_remodeling"
    BATHROOM_REMODELING = "bathroom_remodeling"
    SWIMMING_POOL = "swimming_pool"
    SOLAR_PANELS = "solar_panels"
    ROOFING = "roofing"
    FLOORING = "flooring"
    WINDOWS = "windows"
    SIDING = "siding"
    PAINTING = "painting"
    OTHER = "other"
class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

class ExpirationTime(str, Enum):
    HOURS_24 = "24"
    HOURS_48 = "48"
    HOURS_72 = "72"
# ==================== ADD THESE ENUMS AFTER YOUR EXISTING ENUMS ====================

class ProjectCategory(str, Enum):
    KITCHEN_REMODEL = "kitchen_remodel"
    BATHROOM_REMODEL = "bathroom_remodel"
    HARDSCAPE = "hardscape"
    LANDSCAPE = "landscape"
    DRIVEWAY = "driveway"
    EXTERIOR_PAINT = "exterior_paint"
    INTERIOR_PAINT = "interior_paint"
    ADU = "adu"
    ROOFING = "roofing"
    SOLAR = "solar"
    WINDOWS = "windows"
# ==================== COMPETITION MODELS ====================

class CompetitionType(str, Enum):
    MOST_LEADS = "most_leads"
    MOST_APPROVED = "most_approved"
    MOST_SOLD = "most_sold"
    HIGHEST_VALUE = "highest_value"

class CompetitionStatus(str, Enum):
    UPCOMING = "upcoming"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"



