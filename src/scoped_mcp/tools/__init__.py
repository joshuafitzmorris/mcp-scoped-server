"""Importing this package registers every tool on `scoped_mcp.registry.registry`."""

from scoped_mcp.tools import contracts, invoices, profile

__all__ = ["contracts", "invoices", "profile"]
