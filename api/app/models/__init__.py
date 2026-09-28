"""ORM models. Import this package so every table registers on Base.metadata."""

from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.collection import Collection, CollectionItem
from app.models.identity import BootstrapState, Invitation, PasswordResetToken, User, UserSession
from app.models.index import EmbeddingBudgetDay, EmbeddingCall, EmbeddingUserBudgetDay, IndexEntry, IndexGeneration
from app.models.ingest import Chunk, IngestionJob, IngestStepAttempt, MetadataCandidate, PdfPage, ReferenceEntry, Section
from app.models.library import LibraryItem, PaperVersion, SourceFile
from app.models.search import RagRequestDay, SearchRequestDay, SearchUsage
from app.models.upload import ProcessingAcknowledgement, UploadIntent

__all__ = [
    "AuditEvent",
    "Base",
    "BootstrapState",
    "Collection", "CollectionItem",
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
    "RagRequestDay", "SearchRequestDay", "SearchUsage",
    "User",
    "UserSession",
    "ProcessingAcknowledgement",
    "UploadIntent",
]
