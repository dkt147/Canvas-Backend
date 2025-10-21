"""
Database connection and initialization
"""
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from datetime import datetime
from app.core.config import settings

# Global MongoDB client
client = None
db = None

async def startup_event():
    """Initialize MongoDB connection"""
    global client, db
    try:
        client = MongoClient(settings.MONGODB_URI, server_api=ServerApi('1'))
        client.admin.command('ping')
        db = client.canvassing_app
        print("[OK] Successfully connected to MongoDB!")

        # Initialize collections and indexes
        from app.services.database_init import initialize_database
        await initialize_database(db)

    except Exception as e:
        print(f"[ERROR] Failed to connect to MongoDB: {e}")
        raise

async def shutdown_event():
    """Close MongoDB connection"""
    global client
    if client:
        client.close()
        print("[OK] MongoDB connection closed")

def get_database():
    """Get database instance"""
    return db
