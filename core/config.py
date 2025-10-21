"""
Application configuration and settings
"""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # MongoDB Configuration
    DB_PASSWORD: str = "MySecurePass123%"
    MONGODB_URI: str = "mongodb+srv://canvass_admin:MySecurePass123%21@cluster0.pl1rwjy.mongodb.net/canvass_crm?retryWrites=true&w=majority&appName=Cluster0"

    # JWT Configuration
    SECRET_KEY: str = "your-super-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # QuickBase Configuration
    QUICKBASE_REALM_URL: str = "https://api.quickbase.com"
    QUICKBASE_USER_TOKEN: str = "b2t2cu_h7ii_0_b8u7zjhb7436vxgvbb88c6rusw9"
    QUICKBASE_APP_TOKEN: str = "c233dsbd75yqkxc9qv26vdx3w"
    QUICKBASE_REALM_HOSTNAME: str = "iammanagementsolution.quickbase.com"
    QUICKBASE_TABLE_ID: str = "buct59tvg"
    QUICKBASE_ORG_ID: str = "org_39b6ab4e"

    class Config:
        env_file = ".env"

settings = Settings()
