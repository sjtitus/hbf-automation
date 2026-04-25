"""
Shared bill-entry data structure produced by every vendor pipeline.

Vendor `rules.py` modules apply their own business logic and emit instances
of this dataclass; downstream code (CSV export, processing log) consumes
them uniformly without knowing which vendor produced them.
"""

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class BillEntry:
    vendor: str
    bill_date: str       # MM/DD/YYYY
    due_date: str        # MM/DD/YYYY
    bill_number: str
    category: str
    description: str
    amount: float
    customer: str
    memo: str

    def to_dict(self) -> dict:
        return asdict(self)
