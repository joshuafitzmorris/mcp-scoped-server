"""Record types. Every tenant-owned record carries its tenant, so the call path can check results on the way out."""

from datetime import date
from typing import Literal

from pydantic import BaseModel

InvoiceStatus = Literal["open", "paid", "overdue"]
ContractStatus = Literal["active", "expired", "draft"]


class Invoice(BaseModel):
    id: str
    tenant: str
    customer: str
    amount_cents: int
    status: InvoiceStatus
    issued: date
    due: date
    memo: str


class Contract(BaseModel):
    id: str
    tenant: str
    counterparty: str
    title: str
    status: ContractStatus
    starts: date
    ends: date
    annual_value_cents: int


class Totals(BaseModel):
    tenant: str
    status: InvoiceStatus | None
    count: int
    total_cents: int


class Profile(BaseModel):
    tenant: str
    tenant_name: str
    subject: str
    scopes: list[str]


class User(BaseModel):
    subject: str
    tenant: str
    scopes: list[str]
