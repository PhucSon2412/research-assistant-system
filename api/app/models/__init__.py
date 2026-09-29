"""ORM models. Import this package so every table registers on Base.metadata."""

from app.models.audit import AuditEvent
from app.models.base import Base
from app.models.citation import ReferenceCitationLink
from app.models.collection import Collection, CollectionItem
from app.models.discovery import (DiscoveryDeletedRecord, DiscoveryPaper, DiscoveryPaperLicense,
                                  DiscoveryPaperTopic, DiscoveryRequestClock, DiscoverySource)
from app.models.identity import BootstrapState, Invitation, PasswordResetToken, User, UserSession
from app.models.index import EmbeddingBudgetDay, EmbeddingCall, EmbeddingUserBudgetDay, IndexEntry, IndexGeneration
from app.models.ingest import Chunk, IngestionJob, IngestStepAttempt, MetadataCandidate, PdfPage, ReferenceEntry, Section
from app.models.library import LibraryItem, PaperVersion, SourceFile
from app.models.review import ReviewRequest
from app.models.rights import AccessRights, CommonPublication, DeletionTombstone
from app.models.search import RagRequestDay, SearchRequestDay, SearchUsage
from app.models.upload import ProcessingAcknowledgement, UploadIntent

__all__ = [
    "AccessRights",
    "AuditEvent",
    "Base",
    "BootstrapState",
    "Collection", "CollectionItem", "CommonPublication", "DeletionTombstone",
    "DiscoveryDeletedRecord", "DiscoveryPaper", "DiscoveryPaperLicense", "DiscoveryPaperTopic",
    "DiscoveryRequestClock", "DiscoverySource",
    "EmbeddingBudgetDay", "EmbeddingCall", "EmbeddingUserBudgetDay", "IndexEntry", "IndexGeneration",
    "IngestionJob",
    "IngestStepAttempt",
    "MetadataCandidate",
    "Section",
    "Chunk",
    "PdfPage",
    "ReferenceEntry",
    "ReferenceCitationLink",
    "Invitation",
    "LibraryItem",
    "PaperVersion",
    "PasswordResetToken",
    "SourceFile",
    "ReviewRequest",
    "RagRequestDay", "SearchRequestDay", "SearchUsage",
    "User",
    "UserSession",
    "ProcessingAcknowledgement",
    "UploadIntent",
]
