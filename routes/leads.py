"""
Leads endpoints
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

router = APIRouter(prefix="/api/v1", tags=['Leads'])

@router.post("/api/v1/leads")
async def create_lead_with_notifications(
        lead_data: LeadCreate,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Create a new lead with real-time notifications and QuickBase integration"""
    try:
        leads_collection = db.leads

        # Generate unique lead ID
        lead_id = generate_lead_id(current_user["organization_id"])

        # Save property photo if provided
        photo_id = None
        if lead_data.property_photo_base64:
            photo_id = save_property_photo(lead_data.property_photo_base64, lead_id)

        # Determine initial status
        if current_user["role"] in ["super_admin", "admin_manager"]:
            initial_status = LeadStatus.APPROVED
            approved_by = current_user["username"]
            approval_timestamp = datetime.utcnow()
        else:
            initial_status = LeadStatus.PENDING
            approved_by = None
            approval_timestamp = None

        # Get assigned manager
        assigned_manager = None
        if current_user["role"] == "canvasser":
            users_collection = db.users
            user = users_collection.find_one({"username": current_user["username"]})
            assigned_manager = user.get("manager_id")

        # Create lead document
        lead_doc = {
            "lead_id": lead_id,
            "client_name": lead_data.client_name,
            "phone_number": lead_data.phone_number,
            "email": lead_data.email,
            "address": lead_data.address,
            "marital_status": lead_data.marital_status.value,
            "property_photo_id": photo_id,
            "location": {
                "latitude": lead_data.location.latitude,
                "longitude": lead_data.location.longitude,
                "address": lead_data.location.address,
                "accuracy": lead_data.location.accuracy
            },
            "preferred_appointment_time": lead_data.preferred_appointment_time,
            "products_interested": [product.value for product in lead_data.products_interested],
            "notes": lead_data.notes,
            "lead_status": initial_status.value,
            "created_by": current_user["username"],
            "organization_id": current_user["organization_id"],
            "assigned_manager": assigned_manager,
            "approved_by": approved_by,
            "approval_timestamp": approval_timestamp,
            "created_at": datetime.utcnow(),
            "time_info": datetime.utcnow(),
            "is_active": True
        }

        # ==================== QUICKBASE INTEGRATION ====================
        quickbase_record_id = None
        quickbase_sync_status = "not_applicable"
        quickbase_error = None

        # Check if this is Build Force Inc organization
        if current_user.get("organization_id") == QUICKBASE_ORG_ID:
            print(f"🔄 Syncing lead {lead_id} to QuickBase CRM for Build Force Inc...")

            qb_result = save_lead_to_quickbase(lead_doc, lead_id)

            if qb_result["success"]:
                quickbase_record_id = qb_result["record_id"]
                quickbase_sync_status = "synced"
                print(f"✅ Lead {lead_id} synced to QuickBase - Record ID: {quickbase_record_id}")
            else:
                quickbase_sync_status = "failed"
                quickbase_error = qb_result["error"]
                print(f"⚠️ QuickBase sync failed for lead {lead_id}: {quickbase_error}")

            # Add QuickBase info to lead document
            lead_doc["quickbase_record_id"] = quickbase_record_id
            lead_doc["quickbase_sync_status"] = quickbase_sync_status
            lead_doc["quickbase_sync_error"] = quickbase_error
            lead_doc["quickbase_synced_at"] = datetime.utcnow() if quickbase_record_id else None

        # ==================== SAVE TO MONGODB ====================
        result = leads_collection.insert_one(lead_doc)

        # Award points if canvasser
        points_earned = 0
        if current_user["role"] == "canvasser":
            users_collection = db.users
            users_collection.update_one(
                {"username": current_user["username"]},
                {
                    "$inc": {"points": 10},
                    "$push": {
                        "points_history": {
                            "action": "add",
                            "points": 10,
                            "reason": f"Created lead: {lead_id}",
                            "timestamp": datetime.utcnow()
                        }
                    }
                }
            )
            points_earned = 10

        # ==================== SEND NOTIFICATIONS ====================

        # 1. Notify all organization members (except creator)
        if current_user.get("organization_id"):
            notify_organization_users(
                organization_id=current_user["organization_id"],
                notification_data={
                    "title": "New Lead Created! 🎯",
                    "message": f"{current_user['username']} created a new lead: {lead_data.client_name}",
                    "type": NotificationType.NEW_LEAD.value,
                    "priority": "normal",
                    "data": {
                        "lead_id": lead_id,
                        "client_name": lead_data.client_name,
                        "created_by": current_user["username"]
                    }
                },
                exclude_username=current_user["username"]
            )

        # 2. Notify active competition participants
        competitions_collection = db.competitions
        now = datetime.utcnow()

        active_competitions = competitions_collection.find({
            "status": "active",
            "start_date": {"$lte": now},
            "end_date": {"$gte": now},
            "organization_id": current_user.get("organization_id"),
            "is_active": True
        })

        for comp in active_competitions:
            notify_competition_participants(
                competition_id=comp["competition_id"],
                notification_data={
                    "title": "Competition Update! 🏆",
                    "message": f"New lead in '{comp['title']}' - {current_user['username']} is moving up!",
                    "type": NotificationType.COMPETITION_UPDATE.value,
                    "priority": "high"
                }
            )

        # Build response
        response_data = {
            "message": "Lead created successfully",
            "lead_id": lead_id,
            "database_id": str(result.inserted_id),
            "status": initial_status.value,
            "points_earned": points_earned,
            "notifications_sent": True
        }

        # Add QuickBase info to response if applicable
        if current_user.get("organization_id") == QUICKBASE_ORG_ID:
            response_data["quickbase_sync"] = {
                "status": quickbase_sync_status,
                "record_id": quickbase_record_id,
                "error": quickbase_error
            }

        return response_data

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create lead: {str(e)}"
        )


@router.get("/api/v1/leads")
async def list_leads(
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
        current_user: dict = Depends(get_current_user_from_token)
):
    """List leads with filtering and pagination - ENHANCED VERSION"""
    try:
        leads_collection = db.leads

        # Build filter based on user role
        filter_query = {"is_active": True}

        if current_user["role"] == "super_admin":
            pass
        elif current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            filter_query["organization_id"] = current_user["organization_id"]

            # Get assigned canvassers
            users_collection = db.users
            assigned_canvassers = []
            for user in users_collection.find({"manager_id": current_user["username"]}):
                assigned_canvassers.append(user["username"])

            if assigned_canvassers:
                filter_query["created_by"] = {"$in": assigned_canvassers}
            else:
                filter_query["created_by"] = {"$in": []}
        else:
            filter_query["created_by"] = current_user["username"]

        # Add status filter
        if status:
            filter_query["lead_status"] = status

        # Count total documents
        total_count = leads_collection.count_documents(filter_query)

        # Calculate pagination
        skip = (page - 1) * limit

        # Get leads
        leads = []
        for lead in leads_collection.find(filter_query).skip(skip).limit(limit).sort("created_at", -1):
            # Get creator name
            users_collection = db.users
            creator = users_collection.find_one({"username": lead["created_by"]})
            creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
            lead["created_by"]
            if not creator_name:
                creator_name = lead["created_by"]

            # Build lead object with enhanced fields
            lead_obj = {
                "id": str(lead["_id"]),
                "lead_id": lead["lead_id"],
                "client_name": lead["client_name"],
                "phone_number": lead["phone_number"],
                "email": lead.get("email"),
                "address": lead["address"],
                "marital_status": lead["marital_status"],
                "has_property_photo": bool(lead.get("property_photo_id")),
                "location": lead["location"],
                "preferred_appointment_time": lead["preferred_appointment_time"].isoformat(),
                "products_interested": lead["products_interested"],
                "notes": lead.get("notes"),
                "lead_status": lead["lead_status"],
                "created_by": lead["created_by"],
                "created_by_name": creator_name,
                "organization_id": lead["organization_id"],
                "organization_name": get_organization_name(lead["organization_id"]),
                "created_at": lead["created_at"].isoformat(),

                # ENHANCED FIELDS
                "is_superstar": lead["lead_status"] == "superstar",
                "is_sold": lead["lead_status"] == "sold",
                "can_mark_sold": lead["lead_status"] in ["approved", "superstar"],

                # Superstar info (if applicable)
                "superstar_info": lead.get("superstar_info") if lead["lead_status"] == "superstar" else None,

                # Sale info (if sold)
                "sale_amount": lead.get("sale_amount"),
                "sale_date": lead.get("sale_date").isoformat() if lead.get("sale_date") else None,
                "sale_notes": lead.get("sale_notes"),
                "sold_by": lead.get("sold_by"),

                # Approval info
                "approved_by": lead.get("approved_by"),
                "approval_timestamp": lead.get("approval_timestamp").isoformat() if lead.get(
                    "approval_timestamp") else None,
                "approval_notes": lead.get("approval_notes"),
                "rejection_reason": lead.get("rejection_reason"),

                # QuickBase sync status (if applicable)
                "quickbase_synced": bool(lead.get("quickbase_record_id")),
                "quickbase_record_id": lead.get("quickbase_record_id")
            }

            leads.append(lead_obj)

        # Enhanced summary with superstar count
        summary = {
            "pending": leads_collection.count_documents({**filter_query, "lead_status": "pending"}),
            "approved": leads_collection.count_documents({**filter_query, "lead_status": "approved"}),
            "sold": leads_collection.count_documents({**filter_query, "lead_status": "sold"}),
            "cancelled": leads_collection.count_documents({**filter_query, "lead_status": "cancelled"}),
            "superstar": leads_collection.count_documents({**filter_query, "lead_status": "superstar"})
        }

        return {
            "leads": leads,
            "pagination": {
                "current_page": page,
                "total_pages": (total_count + limit - 1) // limit,
                "total_count": total_count,
                "page_size": limit
            },
            "summary": summary
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list leads: {str(e)}"
        )


@router.post("/api/v1/leads/{lead_id}/approve")
async def approve_lead(
        lead_id: str,
        approval_data: LeadApproval,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Approve or reject a lead"""
    try:
        leads_collection = db.leads

        # Find lead
        if len(lead_id) == 24:
            lead = leads_collection.find_one({"_id": ObjectId(lead_id)})
        else:
            lead = leads_collection.find_one({"lead_id": lead_id})

        if not lead:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lead not found"
            )

        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to approve leads"
            )

        # Check if lead is pending
        if lead["lead_status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only pending leads can be approved or rejected"
            )

        # Update lead status
        update_data = {
            "approved_by": current_user["username"],
            "approval_timestamp": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }

        if approval_data.approve:
            update_data["lead_status"] = "approved"
            if approval_data.notes:
                update_data["approval_notes"] = approval_data.notes

            # Award bonus points to canvasser
            if lead["created_by"]:
                users_collection = db.users
                users_collection.update_one(
                    {"username": lead["created_by"]},
                    {
                        "$inc": {"points": 25},
                        "$push": {
                            "points_history": {
                                "action": "add",
                                "points": 25,
                                "reason": f"Lead approved: {lead['lead_id']}",
                                "timestamp": datetime.utcnow()
                            }
                        }
                    }
                )
        else:
            update_data["lead_status"] = "cancelled"
            update_data["rejection_reason"] = approval_data.rejection_reason

        leads_collection.update_one(
            {"_id": lead["_id"]},
            {"$set": update_data}
        )

        action = "approved" if approval_data.approve else "rejected"
        points_awarded = 25 if approval_data.approve else 0

        return {
            "message": f"Lead {action} successfully",
            "lead_id": lead["lead_id"],
            "new_status": update_data["lead_status"],
            "points_awarded": points_awarded
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to approve lead: {str(e)}"
        )


@router.post("/api/v1/leads/{lead_id}/mark-sold")
async def mark_lead_sold(
        lead_id: str,
        sale_data: LeadSold,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Mark a lead as sold"""
    try:
        leads_collection = db.leads

        # Find lead
        if len(lead_id) == 24:
            lead = leads_collection.find_one({"_id": ObjectId(lead_id)})
        else:
            lead = leads_collection.find_one({"lead_id": lead_id})

        if not lead:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lead not found"
            )

        # Check access permission
        if not check_lead_access(current_user, lead):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to mark this lead as sold"
            )

        # Update lead
        update_data = {
            "lead_status": "sold",
            "sale_amount": sale_data.sale_amount,
            "sale_date": sale_data.sale_date or datetime.utcnow(),
            "sale_notes": sale_data.sale_notes,
            "sold_by": current_user["username"],
            "updated_at": datetime.utcnow()
        }

        leads_collection.update_one(
            {"_id": lead["_id"]},
            {"$set": update_data}
        )

        # Award commission points
        commission_points = int(sale_data.sale_amount * 0.01)
        if lead["created_by"]:
            users_collection = db.users
            users_collection.update_one(
                {"username": lead["created_by"]},
                {
                    "$inc": {"points": commission_points},
                    "$push": {
                        "points_history": {
                            "action": "add",
                            "points": commission_points,
                            "reason": f"Lead sold: {lead['lead_id']} - ${sale_data.sale_amount}",
                            "timestamp": datetime.utcnow()
                        }
                    }
                }
            )

        return {
            "message": "Lead marked as sold successfully",
            "lead_id": lead["lead_id"],
            "sale_amount": sale_data.sale_amount,
            "commission_points": commission_points
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark lead as sold: {str(e)}"
        )


@router.post("/api/v1/leads/{lead_id}/mark-superstar")
async def mark_superstar_lead(
        lead_id: str,
        superstar_data: SuperstarLead,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Mark a lead as superstar"""
    try:
        leads_collection = db.leads

        # Find lead
        if len(lead_id) == 24:
            lead = leads_collection.find_one({"_id": ObjectId(lead_id)})
        else:
            lead = leads_collection.find_one({"lead_id": lead_id})

        if not lead:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Lead not found"
            )

        # Permission check
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to mark superstar leads"
            )

        # Update lead
        superstar_info = {
            "reason": superstar_data.reason,
            "priority_level": superstar_data.priority_level,
            "special_notes": superstar_data.special_notes,
            "marked_by": current_user["username"],
            "marked_at": datetime.utcnow()
        }

        update_data = {
            "lead_status": "superstar",
            "superstar_info": superstar_info,
            "updated_at": datetime.utcnow()
        }

        leads_collection.update_one(
            {"_id": lead["_id"]},
            {"$set": update_data}
        )

        # Award bonus points
        bonus_points = superstar_data.priority_level * 10
        if lead["created_by"]:
            users_collection = db.users
            users_collection.update_one(
                {"username": lead["created_by"]},
                {
                    "$inc": {"points": bonus_points},
                    "$push": {
                        "points_history": {
                            "action": "add",
                            "points": bonus_points,
                            "reason": f"Superstar lead: {lead['lead_id']} - Priority {superstar_data.priority_level}",
                            "timestamp": datetime.utcnow()
                        }
                    }
                }
            )

        return {
            "message": "Lead marked as superstar successfully",
            "lead_id": lead["lead_id"],
            "priority_level": superstar_data.priority_level,
            "bonus_points": bonus_points
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to mark superstar lead: {str(e)}"
        )


@router.get("/api/v1/leads/stats")
async def get_lead_stats(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get lead statistics"""
    try:
        leads_collection = db.leads

        # Build filter based on user role
        filter_query = {"is_active": True}

        if current_user["role"] == "super_admin":
            pass
        elif current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            filter_query["organization_id"] = current_user["organization_id"]
            users_collection = db.users
            assigned_canvassers = [u["username"] for u in
                                   users_collection.find({"manager_id": current_user["username"]})]
            if assigned_canvassers:
                filter_query["created_by"] = {"$in": assigned_canvassers}
        else:
            filter_query["created_by"] = current_user["username"]

        # Get status distribution
        status_stats = {}
        for status in ["pending", "approved", "sold", "cancelled", "superstar"]:
            count = leads_collection.count_documents({**filter_query, "lead_status": status})
            status_stats[status] = count

        # Get total sales
        total_sales = 0
        sold_leads = leads_collection.find({**filter_query, "lead_status": "sold"})
        for lead in sold_leads:
            if lead.get("sale_amount"):
                total_sales += lead["sale_amount"]

        total_leads = sum(status_stats.values())

        return {
            "total_leads": total_leads,
            "status_distribution": status_stats,
            "total_sales_amount": total_sales,
            "average_sale_amount": round(total_sales / max(status_stats["sold"], 1), 2)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get lead statistics: {str(e)}"
        )


@router.get("/api/v1/leads/search")
async def search_leads(
        query: str,
        current_user: dict = Depends(get_current_user_from_token)
):
    """Search leads"""
    try:
        leads_collection = db.leads

        # Build role-based filter
        filter_query = {"is_active": True}

        if current_user["role"] == "super_admin":
            pass
        elif current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            filter_query["organization_id"] = current_user["organization_id"]
            users_collection = db.users
            assigned_canvassers = [u["username"] for u in
                                   users_collection.find({"manager_id": current_user["username"]})]
            if assigned_canvassers:
                filter_query["created_by"] = {"$in": assigned_canvassers}
        else:
            filter_query["created_by"] = current_user["username"]

        # Add search conditions
        search_conditions = [
            {"client_name": {"$regex": query, "$options": "i"}},
            {"phone_number": {"$regex": query, "$options": "i"}},
            {"address": {"$regex": query, "$options": "i"}},
            {"lead_id": {"$regex": query, "$options": "i"}}
        ]

        filter_query["$or"] = search_conditions

        # Get results
        leads = []
        for lead in leads_collection.find(filter_query).limit(50):
            leads.append({
                "id": str(lead["_id"]),
                "lead_id": lead["lead_id"],
                "client_name": lead["client_name"],
                "phone_number": lead["phone_number"],
                "address": lead["address"],
                "lead_status": lead["lead_status"],
                "created_by": lead["created_by"],
                "created_at": lead["created_at"].isoformat()
            })

        return {
            "query": query,
            "results": leads,
            "count": len(leads)
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search leads: {str(e)}"
        )


@router.get("/api/v1/leads/export")
async def export_leads(
        format: str = "json",
        current_user: dict = Depends(get_current_user_from_token)
):
    """Export leads"""
    try:
        leads_collection = db.leads

        # Build filter
        filter_query = {"is_active": True}

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            filter_query["organization_id"] = current_user["organization_id"]
            users_collection = db.users
            assigned_canvassers = [u["username"] for u in
                                   users_collection.find({"manager_id": current_user["username"]})]
            if assigned_canvassers:
                filter_query["created_by"] = {"$in": assigned_canvassers}
        elif current_user["role"] == "canvasser":
            filter_query["created_by"] = current_user["username"]

        # Get leads
        leads = list(leads_collection.find(filter_query).sort("created_at", -1))

        if format.lower() == "csv":
            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)

            headers = ["Lead ID", "Client Name", "Phone", "Status", "Created At"]
            writer.writerow(headers)

            for lead in leads:
                row = [
                    lead["lead_id"],
                    lead["client_name"],
                    lead["phone_number"],
                    lead["lead_status"],
                    lead["created_at"].isoformat()
                ]
                writer.writerow(row)

            output.seek(0)

            return StreamingResponse(
                io.BytesIO(output.getvalue().encode()),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=leads_export.csv"}
            )

        else:
            # Return JSON
            export_data = []
            for lead in leads:
                export_data.append({
                    "lead_id": lead["lead_id"],
                    "client_name": lead["client_name"],
                    "phone_number": lead["phone_number"],
                    "lead_status": lead["lead_status"],
                    "created_at": lead["created_at"].isoformat()
                })

            return {"leads": export_data, "total_count": len(export_data)}

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export leads: {str(e)}"
        )


@router.get("/api/v1/leads/pending-approvals")
async def get_pending_approvals(
        current_user: dict = Depends(get_current_user_from_token)
):
    """Get leads pending approval for managers"""
    try:
        leads_collection = db.leads

        # Only managers and above can see pending approvals
        if current_user["role"] not in ["super_admin", "admin_manager", "manager"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to view pending approvals"
            )

        # Build filter for pending leads
        filter_query = {
            "lead_status": "pending",
            "is_active": True
        }

        if current_user["role"] == "admin_manager":
            filter_query["organization_id"] = current_user["organization_id"]
        elif current_user["role"] == "manager":
            filter_query["organization_id"] = current_user["organization_id"]
            users_collection = db.users
            assigned_canvassers = [u["username"] for u in
                                   users_collection.find({"manager_id": current_user["username"]})]
            if assigned_canvassers:
                filter_query["created_by"] = {"$in": assigned_canvassers}
            else:
                filter_query["created_by"] = {"$in": []}

        pending_leads = []
        for lead in leads_collection.find(filter_query).sort("created_at", 1):
            users_collection = db.users
            creator = users_collection.find_one({"username": lead["created_by"]})
            creator_name = f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip() if creator else \
                lead["created_by"]

            time_pending = datetime.utcnow() - lead["created_at"]
            hours_pending = int(time_pending.total_seconds() / 3600)

            pending_leads.append({
                "id": str(lead["_id"]),
                "lead_id": lead["lead_id"],
                "client_name": lead["client_name"],
                "phone_number": lead["phone_number"],
                "address": lead["address"],
                "products_interested": lead["products_interested"],
                "created_by": lead["created_by"],
                "created_by_name": creator_name,
                "created_at": lead["created_at"].isoformat(),
                "hours_pending": hours_pending,
                "is_urgent": hours_pending > 24
            })

        return {
            "pending_leads": pending_leads,
            "total_count": len(pending_leads),
            "urgent_count": sum(1 for lead in pending_leads if lead["is_urgent"])
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get pending approvals: {str(e)}"
        )


# ==================== PROJECT PORTFOLIO MANAGEMENT ====================
# Step 4: Update the create_project endpoint to check limits


