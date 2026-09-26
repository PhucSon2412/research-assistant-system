"""ORM models. Import this package so every table registers on Base.metadata."""

from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.identity import BootstrapState, Invitation, PasswordResetToken, User, UserSession
from app.models.index import EmbeddingBudgetDay, EmbeddingCall, EmbeddingUserBudgetDay, IndexEntry, IndexGeneration
from app.models.ingest import Chunk, IngestionJob, IngestStepAttempt, MetadataCandidate, PdfPage, ReferenceEntry, Section
from app.models.library import LibraryItem, PaperVersion, SourceFile
from app.models.upload import ProcessingAcknowledgement, UploadIntent

__all__ = [
    "AuditEvent",
    "Base",
    "BootstrapState",
    "EmbeddingBudgetDay", "EmbeddingCall", "EmbeddingUserBudgetDay", "IndexEntry", "IndexGeneration",
    "IngestionJob",
    "IngestStepAttempt",
    "MetadataCandidate",
    "Section",
    "Chunk",
    "PdfPage",
    "ReferenceEntry",
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
