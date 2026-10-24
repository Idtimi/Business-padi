"""Public and partner-facing financial profile routes."""

import os
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import profile

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_db():
    with SessionLocal() as s:
        yield s


@router.get("/profile/shared/{token}", response_class=HTMLResponse)
def view_shared_profile(request: Request, token: str, db: Session = Depends(get_db)):
    """Partner access endpoint to view a trader's verified financial profile."""
    try:
        profile_data, consent, link = profile.access_shared_profile(db, token)
        return templates.TemplateResponse(
            request,
            "shared_profile.html",
            {
                "profile": profile_data,
                "consent": consent,
                "token": token,
            },
        )
    except profile.ConsentAccessDenied as e:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Access Denied",
                "message": str(e),
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )


@router.get("/profile/shared/{token}/pdf")
def download_shared_profile_pdf(token: str, db: Session = Depends(get_db)):
    """Partner access endpoint to download the verified financial profile summary PDF."""
    try:
        profile_data, consent, link = profile.access_shared_profile(db, token)
        pdf_path = profile.generate_profile_pdf(db, consent.business_id, consent.id)
        if not pdf_path or not os.path.exists(pdf_path):
            raise HTTPException(status_code=500, detail="Profile PDF generation failed.")

        partner_clean = consent.partner.replace(" ", "_")
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=f"trade_profile_{partner_clean}.pdf",
        )
    except profile.ConsentAccessDenied as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
