"""
Append-only audit trail for everything the AI/automation layer does.

Every agent vote, scanner decision, auto-created session, auto-approval and
deadline close is recorded so the full chain of automated reasoning is
reconstructable. Callers commit; `log` only flushes.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AuditLog


def log(db: Session, actor: str, action: str, entity: str | None = None, **detail) -> AuditLog:
    """Write one audit entry. `actor` e.g. 'scanner', 'agent:dip', 'auto-trader'."""
    record = AuditLog(
        actor=actor,
        action=action,
        entity=entity,
        detail=json.dumps(detail, default=str) if detail else None,
    )
    db.add(record)
    db.flush()
    return record
