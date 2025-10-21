"""
Security utilities - JWT, password hashing, authentication
"""
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from typing import List
from bson import ObjectId
from app.core.config import settings
from app.core.database import db

# Security setup
security = HTTPBearer()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against hash"""
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    """Create JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def check_permission(current_user: dict, required_roles: List[str]):
    """Check if current user has required role"""
    if current_user.get("role") not in required_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied. Required roles: {required_roles}"
        )

async def get_current_user_from_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current user info from JWT token"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

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
