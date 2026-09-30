from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from database import get_db
from models import PurchaseOrder, POItem, Supplier

router = APIRouter()

@router.get("/po/{po_id}")
async def export_po_pdf(po_id: str, lang: str = Query(default="de", pattern="^(de|en)$"), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
    po = r.scalar_one_or_none()
    if not po: raise HTTPException(404, "PO not found")
    items = (await db.execute(select(POItem).where(POItem.purchase_order_id == po_id).order_by(POItem.position_nr))).scalars().all()
    supplier = {}
    if po.supplier_id:
        s = (await db.execute(select(Supplier).where(Supplier.id == po.supplier_id))).scalar_one_or_none()
        if s: supplier = {"supplier_name": s.name, "supplier_contact": s.contact_name, "supplier_address": s.address, "supplier_country": s.country, "supplier_tax_id": s.tax_id}
    po_data = {"po_number": po.po_number, "job_number": po.job_number, "amount_net": po.amount_net,
        "amount_gross": po.amount_gross, "tax_rate": po.tax_rate, "cost_center": po.cost_center,
        "orderer_name": po.orderer_name, "orderer_email": po.orderer_email,
        "delivery_date": po.delivery_date, "notes": po.notes,
        "issuer_company": "NegotiateX Client", "issuer_tagline": "Procurement Intelligence", **supplier}
    items_list = [{"position_nr": i.position_nr, "description": i.description, "quantity": i.quantity, "unit": i.unit, "unit_price": i.unit_price, "total_price": i.total_price} for i in items]
    try:
        from services.pdf_generator import generate_po_pdf, po_filename
        pdf_bytes = generate_po_pdf(po_data, items_list, lang=lang)
    except Exception as e:
        raise HTTPException(500, f"PDF generation failed: {e}")
    po.pdf_generated_at = datetime.utcnow()
    await db.commit()
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{po_filename(po_data, lang)}"'})

import csv, io, json
from fastapi import Request

@router.get("/datev/{audit_id}")
async def export_datev_csv(audit_id: str, db: AsyncSession = Depends(get_db)):
    """Export purchase orders as DATEV Buchungsstapel CSV."""
    import uuid as _uuid
    r = await db.execute(select(PurchaseOrder).where(PurchaseOrder.audit_id == _uuid.UUID(audit_id)))
    pos = r.scalars().all()
    if not pos:
        raise HTTPException(404, "No purchase orders for this audit")
    output = io.StringIO()
    output.write('"EXTF";700;21;"Buchungsstapel";7;;"";"";"";"";"";"0";"";"";"EUR";"";"";""\n')
    output.write('"Umsatz";"S/H";"WKZ";"Kurs";"Basis";"WKZ Basis";"Konto";"Gegenkonto";"BU";"Belegdatum";"Belegfeld1";"Belegfeld2";"Skonto";"Buchungstext";"Postensperre"\n')
    today = datetime.utcnow().strftime("%d%m")
    for po in pos:
        betrag = str(round(po.amount_gross or 0, 2)).replace(".", ",")
        belegdatum = po.created_at.strftime("%d%m") if po.created_at else today
        buchungstext = ("PO " + str(po.po_number) + " " + (po.notes or "")[:30]).replace('"', '').replace(';', '')
        line = '"' + betrag + '";"S";"EUR";"";"";"";"3400";"1600";"";"' + belegdatum + '";"' + str(po.po_number) + '";"";"0,00";"' + buchungstext + '";"0"\n'
        output.write(line)
    csv_content = output.getvalue()
    return Response(
        content=csv_content.encode("latin-1", errors="replace"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="DATEV_Export_' + audit_id[:8] + '.csv"'}
    )

@router.get("/sap/{audit_id}")
async def export_sap_csv(audit_id: str, db: AsyncSession = Depends(get_db)):
    """Export purchase orders in SAP ME21N-compatible CSV format."""
    import uuid as _uuid
    r = await db.execute(select(PurchaseOrder).where(PurchaseOrder.audit_id == _uuid.UUID(audit_id)))
    pos = r.scalars().all()
    if not pos:
        raise HTTPException(404, "No purchase orders for this audit")
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(["PO_NUMBER","VENDOR","MATERIAL","SHORT_TEXT","QUANTITY","UNIT","NET_PRICE","CURRENCY","PLANT","STORAGE_LOC","PURCH_ORG","PURCH_GROUP","COST_CENTER","DELIVERY_DATE","ITEM_TEXT"])
    for po in pos:
        items_r = await db.execute(select(POItem).where(POItem.purchase_order_id == po.id).order_by(POItem.position_nr))
        items = items_r.scalars().all()
        supplier_name = ""
        if po.supplier_id:
            s = (await db.execute(select(Supplier).where(Supplier.id == po.supplier_id))).scalar_one_or_none()
            if s:
                supplier_name = s.name
        for item in items:
            delivery = po.delivery_date.strftime("%Y%m%d") if po.delivery_date else ""
            writer.writerow([
                po.po_number, supplier_name, f"NTX{item.position_nr:04d}",
                (item.description or "")[:40], item.quantity, item.unit,
                f"{item.unit_price:.2f}", "EUR", "1000", "0001",
                "1000", "001", po.cost_center or "", delivery,
                (po.notes or "")[:50]
            ])
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="SAP_PO_Export_' + audit_id[:8] + '.csv"'}
    )

@router.get("/benchmark/{category}")
async def export_benchmark_csv(category: str, db: AsyncSession = Depends(get_db)):
    """Export benchmark data for a category as CSV."""
    from models import BenchmarkEntry
    r = await db.execute(
        select(BenchmarkEntry).where(BenchmarkEntry.category == category).order_by(BenchmarkEntry.created_at.desc()).limit(500)
    )
    entries = r.scalars().all()
    if not entries:
        raise HTTPException(404, f"No benchmark data for category: {category}")
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["category","metric_type","metric_label","value","currency","region","source","created_at"])
    for e in entries:
        writer.writerow([e.category, e.metric_type, e.metric_label, f"{e.value:.2f}", e.currency, e.region, e.source, e.created_at.strftime("%Y-%m-%d")])
    fn = "Benchmark_" + category.replace(" ", "_") + ".csv"
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'}
    )

