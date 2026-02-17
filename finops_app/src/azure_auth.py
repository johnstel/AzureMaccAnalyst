from __future__ import annotations

import os
from dataclasses import dataclass

from azure.identity import (
    AzureCliCredential,
    AzureDeveloperCliCredential,
    AzurePowerShellCredential,
    ChainedTokenCredential,
    InteractiveBrowserCredential,
    SharedTokenCacheCredential,
    TokenCachePersistenceOptions,
)

from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class AuthConfig:
    tenant_id: str
    client_id: str


def load_auth_config() -> AuthConfig:
    tenant_id = os.getenv("AZURE_TENANT_ID", "common")
    client_id = os.getenv("AZURE_CLIENT_ID", "")
    logger.info("Loaded auth config: tenant=%s, client_id=%s", tenant_id, "(set)" if client_id else "(not set)")
    return AuthConfig(tenant_id=tenant_id, client_id=client_id)


def create_interactive_credential(config: AuthConfig) -> InteractiveBrowserCredential:
    logger.info("Creating InteractiveBrowserCredential (tenant=%s)", config.tenant_id)
    cache_options = TokenCachePersistenceOptions(name="azure-macc-analyst-token-cache")
    kwargs: dict = dict(
        tenant_id=config.tenant_id,
        cache_persistence_options=cache_options,
        timeout=300,
    )
    if config.client_id:
        kwargs["client_id"] = config.client_id
    return InteractiveBrowserCredential(**kwargs)


def create_user_context_credential(config: AuthConfig) -> ChainedTokenCredential:
    """Try cached / CLI contexts first, fall back to interactive browser."""
    logger.info("Creating ChainedTokenCredential (auto-detect chain) for tenant=%s", config.tenant_id)
    cache_options = TokenCachePersistenceOptions(name="azure-macc-analyst-token-cache")
    interactive = create_interactive_credential(config)

    return ChainedTokenCredential(
        AzureCliCredential(tenant_id=config.tenant_id),
        AzureDeveloperCliCredential(tenant_id=config.tenant_id),
        AzurePowerShellCredential(tenant_id=config.tenant_id),
        SharedTokenCacheCredential(tenant_id=config.tenant_id, cache_persistence_options=cache_options),
        interactive,
    )


def create_interactive_only_credential(config: AuthConfig) -> InteractiveBrowserCredential:
    """Force a fresh browser login prompt — ignores cached tokens."""
    logger.info("Creating interactive-only credential (force browser, tenant=%s)", config.tenant_id)
    kwargs: dict = dict(
        tenant_id=config.tenant_id,
        cache_persistence_options=TokenCachePersistenceOptions(name="azure-macc-analyst-token-cache"),
        timeout=300,
        login_hint="",  # forces account picker
    )
    if config.client_id:
        kwargs["client_id"] = config.client_id
    return InteractiveBrowserCredential(**kwargs)
