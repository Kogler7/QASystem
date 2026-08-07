"""Compatibility names for the centralized application-service adapter."""

from external_services.application import (
    ApplicationServiceClient as KnowledgeMapClient,
    ApplicationServiceError as KnowledgeMapError,
    USER_CONTEXT_HEADERS,
    capture_user_headers as set_user_headers,
    get_user_headers,
)

__all__ = [
    "KnowledgeMapClient",
    "KnowledgeMapError",
    "USER_CONTEXT_HEADERS",
    "get_user_headers",
    "set_user_headers",
]
