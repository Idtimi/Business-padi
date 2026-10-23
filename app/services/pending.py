from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import PendingAction, now_iso

TTL_MINUTES = 60


def create(session: Session, business_id: int, user_id: int, kind: str,
           payload: dict, ttl_minutes: int = TTL_MINUTES) -> PendingAction:
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    p = PendingAction(business_id=business_id, user_id=user_id, kind=kind,
                      payload=payload, expires_at=expires)
    session.add(p)
    session.flush()
    return p


def get_for_business(session: Session, business_id: int, pending_id: int) -> PendingAction:
    """Only returns the action if it belongs to this business (spec rule 5)."""
    p = session.get(PendingAction, pending_id)
    if p is None or p.business_id != business_id:
        raise LookupError("pending action not found")
    return p


def is_expired(p: PendingAction) -> bool:
    return p.expires_at is not None and p.expires_at < now_iso()


def cancel(session: Session, business_id: int, pending_id: int) -> PendingAction:
    p = get_for_business(session, business_id, pending_id)
    if p.status == "open":
        p.status = "cancelled"
    session.commit()
    return p