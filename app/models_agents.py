"""
Digitales Team: Protokoll der Arbeitsschritte der NegotiateX-Agenten.

Jede Zeile ist ein abgeschlossener oder laufender Arbeitsschritt eines
Agenten (z.B. "Angebot pruefen"). Daraus zeigt die Team-Seite, woran ein
Agent gerade arbeitet, was er heute erledigt hat und was es gekostet hat.
Die Agenten bleiben dabei, was sie sind: Funktionen, die Entwuerfe und
Pruefergebnisse vorbereiten -- versendet und entschieden wird nur nach
menschlicher Freigabe.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from database import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL nur fuer Schritte ohne Mandantenbezug (z.B. Mail ohne Zuordnung) -- per RLS unsichtbar
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    agent_key = Column(String(30), nullable=False, index=True)
    task = Column(String(255), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    object_ref = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="running")   # running | done | failed
    outcome = Column(Text, nullable=True)                            # kurzes Ergebnis fuer Menschen
    needs_approval = Column(Integer, default=0, nullable=False)      # vorbereitete Entwuerfe, die auf Freigabe warten
    llm_calls = Column(Integer, default=0, nullable=False)
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    started_at = Column(DateTime, server_default=func.now(), index=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
