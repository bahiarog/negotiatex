import hashlib, secrets, logging
from datetime import datetime
from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from database import get_db
from models import PurchaseOrder, POItem, POStatus
from models_requisitions import Requisition, RequisitionItem, RequisitionApproval

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Routing rule ─────────────────────────────────────────────────────────────
# NOTE (intentional simplification): this is a fixed, hard-coded approval
# ladder by amount, not a configurable approval-matrix engine. There is also
# no org chart / role system in this app, so approver name+email per level
# are supplied by the requester themselves at submit time (free text) rather
# than looked up automatically. Both are documented simplifications, not bugs.
def required_levels(amount: Decimal) -> int:
    if amount <= Decimal("1000"):
        return 1
    if amount <= Decimal("10000"):
        return 2
    return 3


class ReqItemCreate(BaseModel):
    description: str
    quantity: float = 1
    unit: str = "Stk."
    unit_price: float = 0


class RequisitionCreate(BaseModel):
    requester_name: str
    requester_email: str
    title: str
    justification: Optional[str] = None
    category: Optional[str] = None
    supplier_id: Optional[str] = None
    currency: str = "EUR"
    items: list[ReqItemCreate] = []


def _req_to_dict(r: Requisition) -> dict:
    return {
        "id": str(r.id), "requester_name": r.requester_name, "requester_email": r.requester_email,
        "title": r.title, "justification": r.justification, "category": r.category,
        "supplier_id": str(r.supplier_id) if r.supplier_id else None, "currency": r.currency,
        "estimated_amount": float(r.estimated_amount) if r.estimated_amount is not None else 0,
        "status": r.status, "purchase_order_id": str(r.purchase_order_id) if r.purchase_order_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    }


@router.post("/create")
async def create_requisition(payload: RequisitionCreate, db: AsyncSession = Depends(get_db)):
    if not payload.title.strip():
        raise HTTPException(400, "Titel ist ein Pflichtfeld.")
    if not payload.items:
        raise HTTPException(400, "Mindestens eine Position ist erforderlich.")

    # Server-side total -- never trust a client-supplied total.
    estimated_amount = Decimal("0")
    item_rows = []
    for it in payload.items:
        qty = Decimal(str(it.quantity))
        price = Decimal(str(it.unit_price))
        total = qty * price
        estimated_amount += total
        item_rows.append((it, qty, price, total))

    req = Requisition(
        requester_name=payload.requester_name, requester_email=payload.requester_email,
        title=payload.title, justification=payload.justification, category=payload.category,
        supplier_id=payload.supplier_id, currency=payload.currency,
        estimated_amount=estimated_amount, status="draft",
    )
    db.add(req)
    await db.flush()
    for it, qty, price, total in item_rows:
        db.add(RequisitionItem(requisition_id=req.id, description=it.description, quantity=qty, unit=it.unit,
                                unit_price=price, total=total))
    await db.commit()
    await db.refresh(req)
    return _req_to_dict(req)


class ApproverEntry(BaseModel):
    level: int
    approver_name: str
    approver_email: str


class SubmitPayload(BaseModel):
    approvers: list[ApproverEntry]


async def _send_approval_email(req: Requisition, approval: RequisitionApproval, raw_token: str):
    from services.email_sender import send_negotiation_email
    link = f"https://negotiatex.ai/approve-requisition?token={raw_token}"
    body = (
        f"Hallo {approval.approver_name},\n\n"
        f"es liegt eine Anforderung zur Freigabe vor (Stufe {approval.level}):\n\n"
        f"Titel: {req.title}\n"
        f"Antragsteller: {req.requester_name}\n"
        f"Betrag (geschätzt): {req.estimated_amount} {req.currency}\n"
        f"Begründung: {req.justification or '-'}\n\n"
        f"Bitte prüfen und freigeben oder ablehnen über folgenden Link:\n\n{link}\n\n"
        f"Vielen Dank!"
    )
    try:
        send_negotiation_email(approval.approver_email, f"Freigabe erforderlich: {req.title}", body)
    except Exception:
        logger.exception("Requisition approval email failed to send")


@router.post("/{req_id}/submit")
async def submit_requisition(req_id: str, payload: SubmitPayload, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Requisition).where(Requisition.id == req_id))
    req = r.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Anforderung nicht gefunden.")
    if req.status not in ("draft",):
        raise HTTPException(400, f"Anforderung befindet sich bereits im Status '{req.status}' und kann nicht erneut eingereicht werden.")

    needed = required_levels(Decimal(str(req.estimated_amount)))
    given_levels = sorted(a.level for a in payload.approvers)
    if given_levels != list(range(1, needed + 1)):
        raise HTTPException(
            400,
            f"Für einen Betrag von {req.estimated_amount} {req.currency} sind genau {needed} Freigabestufe(n) "
            f"erforderlich (Stufen 1..{needed}), erhalten wurden: {given_levels or 'keine'}."
        )

    approvals = []
    for a in sorted(payload.approvers, key=lambda x: x.level):
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        approval = RequisitionApproval(
            requisition_id=req.id, level=a.level, approver_name=a.approver_name,
            approver_email=a.approver_email, status="pending", approval_token_hash=token_hash,
        )
        db.add(approval)
        approvals.append((approval, raw_token))
    req.status = "pending_approval"
    await db.flush()

    # Only the first level gets emailed now; later levels are emailed as earlier ones clear.
    first_approval, first_token = approvals[0]
    await db.commit()
    await db.refresh(req)
    await _send_approval_email(req, first_approval, first_token)

    return _req_to_dict(req)


@router.get("/list")
async def list_requisitions(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Requisition).order_by(Requisition.created_at.desc()))
    return [_req_to_dict(x) for x in r.scalars().all()]


async def _approval_chain(db: AsyncSession, req_id: str):
    r = await db.execute(select(RequisitionApproval).where(RequisitionApproval.requisition_id == req_id).order_by(RequisitionApproval.level))
    return r.scalars().all()


@router.get("/approval-status")
async def approval_status(token: str, db: AsyncSession = Depends(get_db)):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    r = await db.execute(select(RequisitionApproval).where(RequisitionApproval.approval_token_hash == token_hash))
    approval = r.scalar_one_or_none()
    if not approval:
        raise HTTPException(404, "Dieser Freigabe-Link ist ungültig.")

    r2 = await db.execute(select(Requisition).where(Requisition.id == approval.requisition_id))
    req = r2.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Zugehörige Anforderung nicht gefunden.")

    chain = await _approval_chain(db, str(req.id))
    earlier_pending = [a for a in chain if a.level < approval.level and a.status == "pending"]
    is_turn = approval.status == "pending" and not earlier_pending and req.status == "pending_approval"

    items = (await db.execute(select(RequisitionItem).where(RequisitionItem.requisition_id == req.id))).scalars().all()

    return {
        "requisition": _req_to_dict(req),
        "items": [{"description": i.description, "quantity": float(i.quantity), "unit": i.unit,
                    "unit_price": float(i.unit_price), "total": float(i.total)} for i in items],
        "level": approval.level,
        "approval_status": approval.status,
        "is_your_turn": is_turn,
        "reason_not_actionable": None if is_turn else (
            "Diese Freigabestufe wurde bereits entschieden." if approval.status != "pending" else
            "Eine frühere Freigabestufe steht noch aus." if earlier_pending else
            f"Die Anforderung befindet sich im Status '{req.status}' und ist nicht mehr aktionierbar."
        ),
    }


class DecidePayload(BaseModel):
    token: str
    approve: bool
    comment: Optional[str] = None


@router.post("/approval-decide")
async def approval_decide(payload: DecidePayload, db: AsyncSession = Depends(get_db)):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    r = await db.execute(select(RequisitionApproval).where(RequisitionApproval.approval_token_hash == token_hash))
    approval = r.scalar_one_or_none()
    if not approval:
        raise HTTPException(404, "Dieser Freigabe-Link ist ungültig.")

    r2 = await db.execute(select(Requisition).where(Requisition.id == approval.requisition_id))
    req = r2.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Zugehörige Anforderung nicht gefunden.")

    chain = await _approval_chain(db, str(req.id))
    earlier_pending = [a for a in chain if a.level < approval.level and a.status == "pending"]

    if approval.status != "pending":
        raise HTTPException(400, "Diese Freigabestufe wurde bereits entschieden.")
    if earlier_pending:
        raise HTTPException(400, "Eine frühere Freigabestufe steht noch aus. Bitte warten.")
    if req.status != "pending_approval":
        raise HTTPException(400, f"Die Anforderung befindet sich im Status '{req.status}' und ist nicht mehr aktionierbar.")

    from services.email_sender import send_negotiation_email

    approval.comment = payload.comment
    approval.decided_at = datetime.utcnow()

    if not payload.approve:
        approval.status = "rejected"
        req.status = "rejected"
        req.decided_at = datetime.utcnow()
        await db.commit()
        try:
            send_negotiation_email(
                req.requester_email, f"Anforderung abgelehnt: {req.title}",
                f"Hallo {req.requester_name},\n\nIhre Anforderung \"{req.title}\" wurde von "
                f"{approval.approver_name} (Stufe {approval.level}) abgelehnt.\n\n"
                f"Begründung: {payload.comment or '-'}\n",
            )
        except Exception:
            logger.exception("Rejection notification email failed to send")
        return {"status": "rejected"}

    approval.status = "approved"
    max_level = max(a.level for a in chain)
    if approval.level < max_level:
        # Not the final level: activate and email the next level's approver.
        next_approval = next(a for a in chain if a.level == approval.level + 1)
        raw_token = secrets.token_urlsafe(32)
        next_approval.approval_token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        await db.commit()
        await _send_approval_email(req, next_approval, raw_token)
        return {"status": "approved_level", "next_level": next_approval.level}

    # Final level approved.
    req.status = "approved"
    req.decided_at = datetime.utcnow()
    await db.commit()
    try:
        send_negotiation_email(
            req.requester_email, f"Anforderung freigegeben: {req.title}",
            f"Hallo {req.requester_name},\n\nIhre Anforderung \"{req.title}\" wurde vollständig freigegeben "
            f"und kann nun in eine Bestellung umgewandelt werden.\n",
        )
    except Exception:
        logger.exception("Approval notification email failed to send")
    return {"status": "approved"}


async def _po_number(db):
    from sqlalchemy import func as sa_func
    count = (await db.execute(select(sa_func.count(PurchaseOrder.id)))).scalar() or 0
    return f"PO-{datetime.now().year}-{str(count+1).zfill(5)}"


@router.post("/{req_id}/convert-to-po")
async def convert_to_po(req_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Requisition).where(Requisition.id == req_id))
    req = r.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Anforderung nicht gefunden.")
    if req.status != "approved":
        raise HTTPException(400, f"Nur vollständig freigegebene Anforderungen können in eine Bestellung umgewandelt werden (aktueller Status: '{req.status}').")

    items = (await db.execute(select(RequisitionItem).where(RequisitionItem.requisition_id == req_id))).scalars().all()

    po_number = await _po_number(db)
    amount_net = float(req.estimated_amount or 0)
    tax_rate = 19.0
    amount_gross = round(amount_net * (1 + tax_rate / 100), 2)
    po = PurchaseOrder(
        po_number=po_number, supplier_id=req.supplier_id, amount_net=amount_net,
        amount_gross=amount_gross, tax_rate=tax_rate, orderer_name=req.requester_name,
        orderer_email=req.requester_email, notes=f"Erstellt aus Anforderung: {req.title}",
        status=POStatus.draft,
    )
    db.add(po)
    await db.flush()
    for i, item in enumerate(items):
        db.add(POItem(purchase_order_id=po.id, position_nr=i + 1, description=item.description,
                       quantity=float(item.quantity), unit=item.unit, unit_price=float(item.unit_price),
                       total_price=float(item.total)))

    req.status = "converted_to_po"
    req.purchase_order_id = po.id
    await db.commit()
    return {"requisition_id": str(req.id), "purchase_order_id": str(po.id), "po_number": po_number}


# NOTE: this catch-all GET /{req_id} must stay registered after all literal-path
# GET routes above (/list, /approval-status) -- FastAPI matches routes in
# registration order, so a path-param route declared earlier would otherwise
# swallow requests meant for those literal paths.
@router.get("/{req_id}")
async def get_requisition(req_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Requisition).where(Requisition.id == req_id))
    req = r.scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Anforderung nicht gefunden.")
    items = (await db.execute(select(RequisitionItem).where(RequisitionItem.requisition_id == req_id))).scalars().all()
    approvals = await _approval_chain(db, req_id)
    out = _req_to_dict(req)
    out["items"] = [
        {"id": str(i.id), "description": i.description, "quantity": float(i.quantity), "unit": i.unit,
         "unit_price": float(i.unit_price), "total": float(i.total)} for i in items
    ]
    out["approvals"] = [
        {"id": str(a.id), "level": a.level, "approver_name": a.approver_name, "approver_email": a.approver_email,
         "status": a.status, "comment": a.comment, "decided_at": a.decided_at.isoformat() if a.decided_at else None}
        for a in approvals
    ]
    return out
