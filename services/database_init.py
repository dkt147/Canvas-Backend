"""
Database initialization - create indexes and test data
"""
from datetime import datetime
from app.core.security import hash_password

async def initialize_database(db):
    """Initialize database with indexes and test data"""
    try:
        # Create indexes
        await create_indexes(db)

        # Create test data if needed
        await create_test_organizations(db)
        await create_test_users(db)

        print("[OK] Database initialized successfully")
    except Exception as e:
        print(f"[ERROR] Database initialization error: {e}")

async def create_indexes(db):
    """Create all database indexes"""
    try:
        # User indexes
        db.users.create_index("username", unique=True)
        db.users.create_index("email", unique=True)
        db.users.create_index("organization_id")

        # Lead indexes
        db.leads.create_index("lead_id", unique=True)
        db.leads.create_index("organization_id")
        db.leads.create_index("created_by")

        # Competition indexes
        db.competitions.create_index("competition_id", unique=True)
        db.competitions.create_index("organization_id")

        # Reward indexes
        db.rewards.create_index("reward_id", unique=True)
        db.rewards.create_index("organization_id")

        print("[OK] Database indexes created")
    except Exception as e:
        print(f"[WARN] Index creation warning: {e}")

async def create_test_organizations(db):
    """Create test organizations"""
    try:
        test_org = {
            "org_id": "org_001",
            "name": "Test Construction Co",
            "email": "admin@testco.com",
            "max_users": 50,
            "plan": "professional",
            "is_active": True,
            "created_at": datetime.utcnow()
        }

        existing = db.organizations.find_one({"org_id": "org_001"})
        if not existing:
            db.organizations.insert_one(test_org)
            print("[OK] Test organization created")
    except Exception as e:
        print(f"[WARN] Test org creation: {e}")

async def create_test_users(db):
    """Create test users"""
    try:
        test_users = [
            {
                "username": "admin",
                "password": hash_password("admin123"),
                "email": "admin@test.com",
                "role": "super_admin",
                "is_active": True,
                "points": 0,
                "created_at": datetime.utcnow()
            }
        ]

        for user in test_users:
            existing = db.users.find_one({"username": user["username"]})
            if not existing:
                db.users.insert_one(user)
                print(f"[OK] Test user created: {user['username']}")
    except Exception as e:
        print(f"[WARN] Test user creation: {e}")
