"""
Einmalige Neupruefung bestehender Preispositionen nach einer Regelaenderung
(hier: Regelwerk v2 -- Steuerbasis und Stundenzahl nur mit Beleg).

Fuer Positionen ohne gespeicherten Beleg wird im Dokumenttext der Version
nach einer passenden Aussage gesucht (dieselbe Regel wie im Live-Betrieb);
danach wird jede nicht ersetzte Position neu geprueft. Bereits
freigegebene Positionen behalten ihren Status, aber ihre berechneten Werte
und offenen Punkte folgen dem neuen Regelwerk -- jede Wertaenderung wird
im Audit protokolliert, damit nachvollziehbar bleibt, warum sich ein
Vergleich veraendert.

Ausfuehren:
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python maintenance/recheck_mdc_rules.py
"""
import asyncio
import re

from sqlalchemy import select

from database import AdminSessionLocal
from models_mdc import MDCDocumentVersion, MDCLineItem, MDCReviewStatus
from routers.mdc import _apply_check, _enum_val
from services.mdc_extractor import NORMALIZATION_VERSION, _GROSS_RE, _NET_RE, resolve_hours_evidence
from services.mdc_governance import audit

_ROW_PREFIX = re.compile(r"^Zeile \d+:\s*")


def _find_line(text: str, pattern) -> str | None:
    for line in (text or "").splitlines():
        clean = _ROW_PREFIX.sub("", line).strip()
        if clean and pattern.search(clean):
            return clean[:500]
    return None


async def main() -> None:
    changed = found_tax = found_hours = 0
    async with AdminSessionLocal() as db:
        rows = (await db.execute(select(MDCLineItem, MDCDocumentVersion).join(
            MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id,
        ).where(MDCLineItem.review_status != MDCReviewStatus.superseded))).all()
        for item, version in rows:
            before = (str(item.normalized_amount_per_canonical_unit), item.canonical_unit, _enum_val(item.review_status))
            basis = _enum_val(item.tax_basis)
            if basis in ("net", "gross") and not item.tax_evidence and not item.tax_confirmed:
                item.tax_evidence = _find_line(version.extracted_text, _NET_RE if basis == "net" else _GROSS_RE)
                found_tax += item.tax_evidence is not None
            if item.billable_hours_per_day is not None and not item.hours_evidence and not item.hours_confirmed:
                item.hours_evidence = resolve_hours_evidence(None, version.extracted_text)
                found_hours += item.hours_evidence is not None
            _apply_check(item)
            after = (str(item.normalized_amount_per_canonical_unit), item.canonical_unit, _enum_val(item.review_status))
            if after != before:
                changed += 1
                await audit(db, item.tenant_id, "system", "rules_recheck", "line_item", item.id,
                            rules=NORMALIZATION_VERSION, before=list(before), after=list(after))
        await db.commit()
    print(f"{len(rows)} Positionen neu geprueft (Regelwerk {NORMALIZATION_VERSION}); Steuer-Belege gefunden {found_tax}, "
          f"Stunden-Belege gefunden {found_hours}; {changed} mit geaendertem Wert oder Status (im Audit protokolliert).")


if __name__ == "__main__":
    asyncio.run(main())
