
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from app.core.config import settings
from app.core.database import startup_event, shutdown_event

# Import routers
from app.routes import (
    auth,
    users,
    organizations,
    leads,
    projects,
    news,
    time_tracking,
    competitions,
    rewards
)

# Create FastAPI app
app = FastAPI(
    title="Canvassing App API",
    version="1.0.0",
    description="Complete CRM system for canvassing operations"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(organizations.router)
app.include_router(leads.router)
app.include_router(projects.router)
app.include_router(news.router)
app.include_router(time_tracking.router)
app.include_router(competitions.router)
app.include_router(rewards.router)

# Event handlers
app.add_event_handler("startup", startup_event)
app.add_event_handler("shutdown", shutdown_event)

@app.get("/")
async def root():
    return {
        "message": "Canvassing App API v1.0.0",
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=True)
