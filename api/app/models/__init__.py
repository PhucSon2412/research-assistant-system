"""ORM models. Import this package so every table registers on Base.metadata."""

from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.identity import BootstrapState, Invitation, PasswordResetToken, User, UserSession
from app.models.ingest import IngestionJob
from app.models.library import LibraryItem, PaperVersion, SourceFile
from app.models.upload import ProcessingAcknowledgement, UploadIntent

__all__ = [
    "AuditEvent",
    "Base",
    "BootstrapState",
    "IngestionJob",
    "Invitation",
    "LibraryItem",
    "PaperVersion",
    "PasswordResetToken",
    "SourceFile",
    "User",
    "UserSession",
    "ProcessingAcknowledgement",
    "UploadIntent",
]
