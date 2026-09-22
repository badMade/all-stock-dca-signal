"""Role-based document retrieval engine with enforced metadata authorization checks.

Layer 2 of the defense-in-depth architecture: guarantees that documents outside
a user's department, role, or classification clearance are pruned in code before
any context can ever be injected into an LLM prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set


@dataclass
class Document:
    """Represents a discrete knowledge document in the internal corpus."""

    id: str
    content: str
    department: str
    classification_level: str = "internal"
    allowed_roles: Optional[Set[str]] = None
    title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize fields after initialization."""
        self.department = self.department.strip()
        self.classification_level = self.classification_level.strip().lower()
        if self.allowed_roles is not None:
            self.allowed_roles = {r.strip().lower() for r in self.allowed_roles}


@dataclass
class UserContext:
    """Identity and authorization context for the querying user."""

    user_id: str
    role: str
    department: str
    clearance_level: str = "internal"

    def __post_init__(self) -> None:
        """Normalize fields after initialization."""
        self.role = self.role.strip().lower()
        self.department = self.department.strip()
        self.clearance_level = self.clearance_level.strip().lower()


class RetrievalEngine:
    """In-memory retrieval engine enforcing role-based access control (RBAC).

    Authorization filtering is strictly computed in Python code prior to any context
    synthesis. The model never sees or receives unauthorized documents.
    """

    DEFAULT_CLEARANCE_HIERARCHY: Dict[str, int] = {
        "public": 0,
        "internal": 1,
        "confidential": 2,
        "restricted": 3,
    }

    def __init__(
        self,
        documents: Optional[Sequence[Document]] = None,
        clearance_hierarchy: Optional[Dict[str, int]] = None,
        universal_departments: Optional[Set[str]] = None,
    ) -> None:
        """Initialize the retrieval engine with documents and access boundaries.

        Args:
            documents: Initial collection of documents.
            clearance_hierarchy: Mapping of classification level names to numeric ranks.
                Defaults to DEFAULT_CLEARANCE_HIERARCHY.
            universal_departments: Departments accessible across all user departments
                (e.g., 'General', 'Company-Wide', 'Public').
        """
        self._documents: List[Document] = list(documents) if documents is not None else []
        self._clearance_hierarchy = (
            clearance_hierarchy if clearance_hierarchy is not None else dict(self.DEFAULT_CLEARANCE_HIERARCHY)
        )
        self._universal_departments = (
            {d.lower() for d in universal_departments}
            if universal_departments is not None
            else {"general", "all", "company-wide", "public"}
        )

    def add_document(self, document: Document) -> None:
        """Add a document to the index.

        Args:
            document: Document instance to add.
        """
        self._documents.append(document)

    def is_authorized(self, document: Document, user: UserContext) -> bool:
        """Verify whether a user is authorized to read a document.

        Access rule:
        1. Clearance rank: user's clearance level rank must be >= document's classification level rank.
        2. Role & Department bounds:
           - If document specifies `allowed_roles`, user's role must be in `allowed_roles`.
           - Else, document department must be universal OR match user's department.

        Args:
            document: Target document being evaluated.
            user: Requesting user context.

        Returns:
            True if authorized, False otherwise.
        """
        # 1. Clearance check
        doc_rank = self._clearance_hierarchy.get(document.classification_level, 999)
        user_rank = self._clearance_hierarchy.get(user.clearance_level, -1)
        if user_rank < doc_rank:
            return False

        # 2. Allowed roles check (if explicitly defined on document)
        if document.allowed_roles is not None:
            return user.role in document.allowed_roles

        # 3. Department matching (if allowed_roles is not explicitly restricted)
        doc_dept = document.department.lower()
        if doc_dept in self._universal_departments:
            return True

        return doc_dept == user.department.lower()

    def retrieve(
        self,
        query: str,
        user: UserContext,
        top_k: int = 5,
    ) -> List[Document]:
        """Retrieve relevant documents for a user, enforcing RBAC filtering in code.

        Args:
            query: User's search query string.
            user: User context with credentials and roles.
            top_k: Maximum number of documents to return.

        Returns:
            List of permitted Document instances ranked by term overlap.
        """
        # Step 1: Strict RBAC filtering in code
        authorized_docs = [doc for doc in self._documents if self.is_authorized(doc, user)]

        if not authorized_docs or not query.strip():
            return []

        # Step 2: Relevance scoring (token overlap) on authorized documents only
        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return authorized_docs[:top_k]

        scored_docs: List[tuple[float, Document]] = []
        for doc in authorized_docs:
            doc_text = f"{doc.title} {doc.content}".lower()
            doc_tokens = set(re.findall(r"\w+", doc_text))
            intersection = query_tokens.intersection(doc_tokens)
            score = len(intersection) / max(1, len(query_tokens))
            scored_docs.append((score, doc))

        # Sort by relevance score descending
        scored_docs.sort(key=lambda x: x[0], reverse=True)
        return [doc for score, doc in scored_docs if score > 0][:top_k]
