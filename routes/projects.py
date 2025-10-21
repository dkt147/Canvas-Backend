"""
Projects endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Projects'])

@router.post("/api/v1/projects")
async def create_project(
        project_data: ProjectCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new project with organization limits"""
    try:
        # Permission check - only admin_manager and super_admin can create projects
        if not check_project_permission(current_user, "create"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to create projects"
            )

        # Handle organization_id for super_admin
        org_id = current_user.get("organization_id")
        if current_user["role"] == "super_admin" and not org_id:
            # For super_admin without organization, use default or first available
            orgs_collection = db.organizations
            first_org = orgs_collection.find_one({"is_active": True})
            if first_org:
                org_id = first_org["org_id"]
            else:
                org_id = "default_org"

        # CHECK PROJECT LIMITS - NEW CODE
        if org_id and org_id != "default_org":
            project_limit_check = check_organization_limits(org_id, "max_projects", 1)
            if not project_limit_check["allowed"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Project limit exceeded: {project_limit_check['message']}"
                )

            # CHECK IMAGE LIMITS - NEW CODE
            image_count = len(project_data.project_images) if project_data.project_images else 0
            if image_count > 0:
                image_limit_check = check_project_image_limits(org_id, 0, image_count)
                if not image_limit_check["allowed"]:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Project image limit exceeded: {image_limit_check['message']}"
                    )

        projects_collection = db.projects

        # Generate unique project ID
        project_id = generate_project_id(org_id)

        # Process uploaded images (existing code continues...)
        image_ids = []
        if project_data.project_images:
             for img in project_data.project_images:
                image_id = save_project_image(
                    img.image_data,
                    project_id,
                    img.caption  # ← FIXED: was 'caption', now 'img.caption'
                )
                if image_id:
                    image_ids.append({
                        "image_id": image_id,
                        "caption": img.caption,
                        "is_primary": img.is_primary
                    })

        # Convert date to datetime for MongoDB compatibility
        completion_datetime = datetime.combine(project_data.completion_date, datetime.min.time())

        # Create project document
        project_doc = {
            "project_id": project_id,
            "title": project_data.title,
            "category": project_data.category.value,
            "description": project_data.description,
            "image_urls": project_data.image_urls or [],  # Legacy support
            "project_images": image_ids,  # New image system
            "completion_date": completion_datetime,
            "location": project_data.location,
            "is_featured": project_data.is_featured,
            "organization_id": org_id,
            "created_by": current_user["username"],
            "created_at": datetime.utcnow(),
            "is_active": True
        }

        result = projects_collection.insert_one(project_doc)

        return {
            "message": "Project created successfully",
            "project_id": project_id,
            "database_id": str(result.inserted_id),
            "images_uploaded": len(image_ids),
            "organization_id": org_id,
            "limit_info": project_limit_check if org_id != "default_org" else None
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create project: {str(e)}"
        )
# 5. Fix the list_projects function to handle date conversion:
@router.get("/api/v1/projects")
async def list_projects(
        category: Optional[str] = None,
        featured_only: bool = False,
        page: int = 1,
        limit: int = 20,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List projects with filtering"""
    try:
        projects_collection = db.projects

        # Build filter - projects are visible to all users in the organization
        filter_query = {"is_active": True}

        # Handle organization filtering
        if current_user["role"] == "super_admin":
            # Super admin can see all projects
            pass
        elif current_user.get("organization_id"):
            filter_query["organization_id"] = current_user["organization_id"]
        else:
            # User has no organization, show no projects
            filter_query["organization_id"] = {"$exists": False}

        if category:
            filter_query["category"] = category

        if featured_only:
            filter_query["is_featured"] = True

        # Count total documents
        total_count = projects_collection.count_documents(filter_query)

        # Calculate pagination
        skip = (page - 1) * limit

        # Get projects
        projects = []
        for project in projects_collection.find(filter_query).skip(skip).limit(limit).sort("created_at", -1):

            # Get creator info
            users_collection = db.users
            creator = users_collection.find_one({"username": project["created_by"]})
            creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
            project["created_by"]

            # Get image data for project images
            project_images_with_data = []
            if project.get("project_images"):
                images_collection = db.project_images
                for img_info in project["project_images"]:
                    img_doc = images_collection.find_one({"image_id": img_info["image_id"]})
                    if img_doc:
                        project_images_with_data.append({
                            "image_id": img_info["image_id"],
                            "image_data": f"data:image/jpeg;base64,{img_doc['image_data']}",
                            "caption": img_info.get("caption"),
                            "is_primary": img_info.get("is_primary", False)
                        })

            # FIX: Handle completion_date conversion
            completion_date = project["completion_date"]
            if isinstance(completion_date, datetime):
                completion_date_str = completion_date.date().isoformat()
            else:
                completion_date_str = completion_date.isoformat() if completion_date else None

            projects.append({
                "id": str(project["_id"]),
                "project_id": project["project_id"],
                "title": project["title"],
                "category": project["category"],
                "description": project["description"],
                "image_urls": project.get("image_urls", []),  # Legacy
                "project_images": project_images_with_data,  # New system
                "completion_date": completion_date_str,
                "location": project["location"],
                "is_featured": project["is_featured"],
                "created_by": project["created_by"],
                "created_by_name": creator_name,
                "organization_id": project.get("organization_id"),
                "created_at": project["created_at"].isoformat()
            })

        return {
            "projects": projects,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "categories": [cat.value for cat in ProjectCategory]
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list projects: {str(e)}"
        )
@router.get("/api/v1/projects/categories")
async def get_project_categories():
    """Get all available project categories - NO AUTH REQUIRED"""
    return {
        "categories": [
            {"value": "kitchen_remodel", "label": "Kitchen Remodel"},
            {"value": "bathroom_remodel", "label": "Bathroom Remodel"},
            {"value": "hardscape", "label": "Hardscape"},
            {"value": "landscape", "label": "Landscape"},
            {"value": "driveway", "label": "Driveway"},
            {"value": "exterior_paint", "label": "Exterior Paint"},
            {"value": "interior_paint", "label": "Interior Paint"},
            {"value": "adu", "label": "ADU"},
            {"value": "roofing", "label": "Roofing"},
            {"value": "solar", "label": "Solar"},
            {"value": "windows", "label": "Windows"}
        ]
    }
@router.get("/api/v1/projects/{project_id}")
async def get_project(
        project_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get a specific project by ID"""
    try:
        projects_collection = db.projects

        # Find project
        if len(project_id) == 24:
            project = projects_collection.find_one({"_id": ObjectId(project_id)})
        else:
            project = projects_collection.find_one({"project_id": project_id})

        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        # Check if user can view this project
        if current_user["role"] != "super_admin" and project.get("organization_id") != current_user["organization_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view this project"
            )

        # Get creator info
        users_collection = db.users
        creator = users_collection.find_one({"username": project["created_by"]})
        creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
        project["created_by"]

        # Get full image data
        project_images_with_data = []
        if project.get("project_images"):
            images_collection = db.project_images
            for img_info in project["project_images"]:
                img_doc = images_collection.find_one({"image_id": img_info["image_id"]})
                if img_doc:
                    project_images_with_data.append({
                        "image_id": img_info["image_id"],
                        "image_data": f"data:image/jpeg;base64,{img_doc['image_data']}",
                        "caption": img_info.get("caption"),
                        "is_primary": img_info.get("is_primary", False),
                        "uploaded_at": img_doc["uploaded_at"].isoformat()
                    })

        return {
            "id": str(project["_id"]),
            "project_id": project["project_id"],
            "title": project["title"],
            "category": project["category"],
            "description": project["description"],
            "image_urls": project.get("image_urls", []),
            "project_images": project_images_with_data,
            "completion_date": project["completion_date"].isoformat(),
            "location": project["location"],
            "is_featured": project["is_featured"],
            "created_by": project["created_by"],
            "created_by_name": creator_name,
            "organization_id": project["organization_id"],
            "organization_name": get_organization_name(project["organization_id"]),
            "created_at": project["created_at"].isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get project: {str(e)}"
        )



# 2. Fix the update_project function as well:
@router.put("/api/v1/projects/{project_id}")
async def update_project(
        project_id: str,
        project_update: ProjectUpdate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Update a project"""
    try:
        # Permission check
        if not check_project_permission(current_user, "update"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to update projects"
            )

        projects_collection = db.projects

        # Find project
        if len(project_id) == 24:
            project = projects_collection.find_one({"_id": ObjectId(project_id)})
            project_filter = {"_id": ObjectId(project_id)}
        else:
            project = projects_collection.find_one({"project_id": project_id})
            project_filter = {"project_id": project_id}

        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        # Check organization permission
        if current_user["role"] != "super_admin" and project.get("organization_id") != current_user["organization_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only update projects from your organization"
            )

        # Build update document
        update_data = {}

        if project_update.title is not None:
            update_data["title"] = project_update.title
        if project_update.category is not None:
            update_data["category"] = project_update.category.value
        if project_update.description is not None:
            update_data["description"] = project_update.description
        if project_update.image_urls is not None:
            update_data["image_urls"] = project_update.image_urls
        if project_update.completion_date is not None:
            # FIX: Convert date to datetime
            update_data["completion_date"] = datetime.combine(project_update.completion_date, datetime.min.time())
        if project_update.location is not None:
            update_data["location"] = project_update.location
        if project_update.is_featured is not None:
            update_data["is_featured"] = project_update.is_featured

        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields provided for update"
            )

        update_data["updated_at"] = datetime.utcnow()
        update_data["updated_by"] = current_user["username"]

        # Perform update
        projects_collection.update_one(project_filter, {"$set": update_data})

        return {
            "message": "Project updated successfully",
            "project_id": project.get("project_id"),
            "updated_fields": list(update_data.keys())
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update project: {str(e)}"
        )

@router.delete("/api/v1/projects/{project_id}")
async def delete_project(
        project_id: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Delete a project (soft delete)"""
    try:
        # Permission check
        if not check_project_permission(current_user, "delete"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to delete projects"
            )

        projects_collection = db.projects

        # Find project
        if len(project_id) == 24:
            project = projects_collection.find_one({"_id": ObjectId(project_id)})
            project_filter = {"_id": ObjectId(project_id)}
        else:
            project = projects_collection.find_one({"project_id": project_id})
            project_filter = {"project_id": project_id}

        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        # Check organization permission
        if current_user["role"] != "super_admin" and project.get("organization_id") != current_user["organization_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only delete projects from your organization"
            )

        # Soft delete
        update_data = {
            "is_active": False,
            "deleted_at": datetime.utcnow(),
            "deleted_by": current_user["username"]
        }

        projects_collection.update_one(project_filter, {"$set": update_data})

        return {
            "message": "Project deleted successfully",
            "project_id": project.get("project_id")
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete project: {str(e)}"
        )


@router.post("/api/v1/projects/{project_id}/images")
async def add_project_images(
        project_id: str,
        images: List[ProjectImageUpload],
        current_user: dict = Depends(get_current_user_from_token)
):
    """Add images to an existing project"""
    try:
        # Permission check
        if not check_project_permission(current_user, "update"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to add project images"
            )

        projects_collection = db.projects

        # Find project
        if len(project_id) == 24:
            project = projects_collection.find_one({"_id": ObjectId(project_id)})
            project_filter = {"_id": ObjectId(project_id)}
        else:
            project = projects_collection.find_one({"project_id": project_id})
            project_filter = {"project_id": project_id}

        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found"
            )

        # Save new images
        new_image_ids = []
        for img in images:
            image_id = save_project_image(
                img.image_data,
                project.get("project_id", project_id),
                img.caption
            )
            if image_id:
                new_image_ids.append({
                    "image_id": image_id,
                    "caption": img.caption,
                    "is_primary": img.is_primary
                })

        # Add to project
        if new_image_ids:
            projects_collection.update_one(
                project_filter,
                {
                    "$push": {"project_images": {"$each": new_image_ids}},
                    "$set": {"updated_at": datetime.utcnow()}
                }
            )

        return {
            "message": "Images added successfully",
            "project_id": project.get("project_id"),
            "images_added": len(new_image_ids)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add project images: {str(e)}"
        )

@router.get("/api/v1/projects/search")
async def search_projects(
        query: str,
        category: Optional[str] = None,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Search projects"""
    try:
        projects_collection = db.projects

        # Build filter
        filter_query = {"is_active": True}

        if current_user["role"] != "super_admin":
            filter_query["organization_id"] = current_user["organization_id"]

        if category:
            filter_query["category"] = category

        # Add search conditions
        search_conditions = [
            {"title": {"$regex": query, "$options": "i"}},
            {"description": {"$regex": query, "$options": "i"}},
            {"location": {"$regex": query, "$options": "i"}},
            {"project_id": {"$regex": query, "$options": "i"}}
        ]

        filter_query["$or"] = search_conditions

        # Get results
        projects = []
        for project in projects_collection.find(filter_query).limit(20):
            projects.append({
                "id": str(project["_id"]),
                "project_id": project["project_id"],
                "title": project["title"],
                "category": project["category"],
                "location": project["location"],
                "is_featured": project["is_featured"],
                "completion_date": project["completion_date"].date().isoformat() if isinstance(project["completion_date"], datetime) else project["completion_date"].isoformat(),
                "image_count": len(project.get("project_images", [])) + len(project.get("image_urls", []))
            })

        return {
            "query": query,
            "category": category,
            "results": projects,
            "count": len(projects)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search projects: {str(e)}"
        )


# ==================== NEWS MANAGEMENT ====================


