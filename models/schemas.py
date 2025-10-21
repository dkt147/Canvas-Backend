"""
Pydantic models for request/response validation
"""
from pydantic import BaseModel, EmailStr
from datetime import datetime, date
from typing import Optional, List
from app.models.enums import *

# ==================== PYDANTIC MODELS ====================
class LocationPoint(BaseModel):
    latitude: float
    longitude: float
    accuracy: Optional[float] = None
    speed: Optional[float] = None  # km/h
    heading: Optional[float] = None  # degrees
    altitude: Optional[float] = None  # meters
    timestamp: Optional[datetime] = None

class PathSegment(BaseModel):
    start_point: LocationPoint
    end_point: LocationPoint
    distance_meters: float
    duration_seconds: float
    average_speed: Optional[float] = None

class LiveTrackingUpdate(BaseModel):
    location: LocationPoint
    activity_type: Optional[str] = "moving"  # moving, stationary, canvassing, break
    notes: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str
    user_info: dict


# Update OrganizationCreate model
class OrganizationCreate(BaseModel):
    name: str
    email: EmailStr
    max_users: int = 20
    industry: Optional[str] = "Construction"
    address: Optional[str] = None
    phone: Optional[str] = None
    plan: OrganizationPlan = OrganizationPlan.BASIC  # Add this line

# Update OrganizationUpdate model
class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    max_users: Optional[int] = None
    industry: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    plan: Optional[OrganizationPlan] = None  # Add this line

# User Management Models
class UserCreate(BaseModel):
    username: str
    password: str
    email: EmailStr
    role: str  # super_admin, admin_manager, manager, canvasser
    organization_id: Optional[str] = None
    manager_id: Optional[str] = None  # For canvassers - who is their manager
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    terms_accepted: bool = True
    points: int = 0


class UserUpdate(BaseModel):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    organization_id: Optional[str] = None
    manager_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    points: Optional[int] = None
    terms_accepted: Optional[bool] = None  # Add this line



class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordReset(BaseModel):
    new_password: str

# Add these Pydantic models after your existing models
class BreakStart(BaseModel):
    break_type: BreakType = BreakType.PERSONAL
    reason: Optional[str] = None
    expected_duration_minutes: Optional[int] = 30  # Expected break duration

class BreakEnd(BaseModel):
    notes: Optional[str] = None

# Lead Models
class LocationInfo(BaseModel):
    latitude: float
    longitude: float
    address: str
    accuracy: Optional[float] = None

# ==================== PYDANTIC MODELS ====================

class RewardCreate(BaseModel):
    name: str
    description: str
    category: RewardCategory
    points_required: int
    stock_quantity: Optional[int] = None  # None = unlimited
    image_url: Optional[str] = None
    image_base64: Optional[str] = None  # For image upload
    is_featured: bool = False
    terms_conditions: Optional[str] = None
    estimated_delivery_days: Optional[int] = 7
    is_active: bool = True

class RewardUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[RewardCategory] = None
    points_required: Optional[int] = None
    stock_quantity: Optional[int] = None
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    is_featured: Optional[bool] = None
    terms_conditions: Optional[str] = None
    estimated_delivery_days: Optional[int] = None
    is_active: Optional[bool] = None
    status: Optional[RewardStatus] = None

class RedemptionRequest(BaseModel):
    reward_id: str
    shipping_address: str
    contact_phone: str
    special_instructions: Optional[str] = None

class RedemptionUpdate(BaseModel):
    status: RedemptionStatus
    admin_notes: Optional[str] = None
    tracking_number: Optional[str] = None


class LeadCreate(BaseModel):
    client_name: str
    phone_number: str
    email: Optional[str] = None
    address: str
    marital_status: MaritalStatus
    property_photo_base64: Optional[str] = None
    location: LocationInfo
    preferred_appointment_time: datetime
    products_interested: List[ProductType]
    notes: Optional[str] = None


class LeadUpdate(BaseModel):
    client_name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    marital_status: Optional[MaritalStatus] = None
    property_photo_base64: Optional[str] = None
    preferred_appointment_time: Optional[datetime] = None
    products_interested: Optional[List[ProductType]] = None
    notes: Optional[str] = None
    lead_status: Optional[LeadStatus] = None


class LeadApproval(BaseModel):
    approve: bool
    rejection_reason: Optional[str] = None
    notes: Optional[str] = None


class LeadSold(BaseModel):
    sale_amount: float
    sale_date: Optional[datetime] = None
    sale_notes: Optional[str] = None


class SuperstarLead(BaseModel):
    reason: str
    priority_level: int = 5
    special_notes: Optional[str] = None


class ParticipantSelection(BaseModel):
    username: str
    user_id: str
class CompetitionCreate(BaseModel):
    title: str
    description: str
    competition_type: CompetitionType
    start_date: datetime
    end_date: datetime
    prize_description: str
    prize_points: int = 0
    target_roles: List[str] = ["canvasser"]
    organization_specific: bool = True
    min_participants: int = 2
    is_active: bool = True
    participant_selection_mode: str = "all"  # "all", "roles", "specific"
    selected_participants: Optional[List[str]] = None  # List of usernames


class CompetitionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    prize_description: Optional[str] = None
    prize_points: Optional[int] = None
    is_active: Optional[bool] = None
    participant_selection_mode: Optional[str] = None
    selected_participants: Optional[List[str]] = None
# ==================== NEWS MANAGEMENT MODELS ====================

# Update NewsCreate model to support base64 image upload
class NewsCreate(BaseModel):
    title: str
    content: str
    image_url: Optional[str] = None  # Keep for backward compatibility
    image_base64: Optional[str] = None  # NEW: For base64 image upload
    priority: Priority = Priority.MEDIUM
    expiration_hours: ExpirationTime = ExpirationTime.HOURS_24
    is_pinned: bool = False
    is_active: bool = True
    target_roles: List[str] = []
    organization_specific: bool = True

# Update NewsUpdate model as well
class NewsUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    image_url: Optional[str] = None
    image_base64: Optional[str] = None  # NEW: For base64 image upload
    priority: Optional[Priority] = None
    expiration_hours: Optional[ExpirationTime] = None
    is_pinned: Optional[bool] = None
    is_active: Optional[bool] = None
    target_roles: Optional[List[str]] = None


# ==================== ADD THESE PYDANTIC MODELS AFTER YOUR EXISTING MODELS ====================

class ProjectImageUpload(BaseModel):
    image_data: str  # base64 encoded image
    caption: Optional[str] = None
    is_primary: bool = False

class ProjectCreate(BaseModel):
    title: str
    category: ProjectCategory
    description: str
    image_urls: Optional[List[str]] = []  # For backward compatibility
    project_images: Optional[List[ProjectImageUpload]] = []  # For new base64 uploads
    completion_date: date
    location: str
    is_featured: bool = False

class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[ProjectCategory] = None
    description: Optional[str] = None
    image_urls: Optional[List[str]] = None
    completion_date: Optional[date] = None
    location: Optional[str] = None
    is_featured: Optional[bool] = None

class ProjectImageUpdate(BaseModel):
    image_id: str
    caption: Optional[str] = None
    is_primary: Optional[bool] = None

# ==================== ADD THESE HELPER FUNCTIONS AFTER YOUR EXISTING HELPER FUNCTIONS ====================

