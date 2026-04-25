"""
Badger State Western business rules.

Maps parsed invoice data + validated customer to a BillEntry per HBF's
Badger conventions:

  * Bill date  : if invoice_date and ship_date share a calendar month, use
                 invoice_date; otherwise use ship_date.
  * Due date   : past_due_date − 1 day.
  * Category   : looked up by shipper in SHIPPER_CATEGORY_MAP.
"""

import logging
from datetime import timedelta
from typing import Dict

from ...bill_entry import BillEntry


logger = logging.getLogger(__name__)


VENDOR_NAME = 'Badger State Western, Inc.'

REQUIRED_FIELDS = (
    'invoice_number', 'invoice_date', 'ship_date',
    'shipper', 'consignee', 'so_number',
    'total_amount', 'past_due_date',
)

SHIPPER_CATEGORY_MAP = {
    'Midwest Refrigerated Services': 'Product Delivery - Customer:Outbound Transport - MRS',
    'Old Wisconsin Sausage Company': '5127 Product Delivery - Customer:Old Wisconsin',
    'DairyFood USA': '5128 Product Delivery - Customer:Dairyfood',
}


def build_bill_entry(invoice_data: Dict, customer_name: str) -> BillEntry:
    """Build a BillEntry for a single Badger invoice."""
    return BillEntry(
        vendor=VENDOR_NAME,
        bill_date=_calculate_bill_date(invoice_data),
        due_date=_calculate_due_date(invoice_data),
        bill_number=invoice_data['invoice_number'],
        category=_determine_category(invoice_data['shipper']),
        description=invoice_data['so_number'],
        amount=invoice_data['total_amount'],
        customer=customer_name,
        memo=invoice_data['so_number'],
    )


def _calculate_bill_date(invoice_data: Dict) -> str:
    invoice_date = invoice_data['invoice_date']
    ship_date = invoice_data['ship_date']
    same_month = (invoice_date.year == ship_date.year
                  and invoice_date.month == ship_date.month)
    chosen = invoice_date if same_month else ship_date
    logger.debug("bill_date branch=%s invoice=%s ship=%s -> %s",
                 'same_month' if same_month else 'cross_month',
                 invoice_date.strftime('%m/%d/%Y'),
                 ship_date.strftime('%m/%d/%Y'),
                 chosen.strftime('%m/%d/%Y'))
    return chosen.strftime('%m/%d/%Y')


def _calculate_due_date(invoice_data: Dict) -> str:
    due = invoice_data['past_due_date'] - timedelta(days=1)
    logger.debug("due_date past_due=%s -> %s",
                 invoice_data['past_due_date'].strftime('%m/%d/%Y'),
                 due.strftime('%m/%d/%Y'))
    return due.strftime('%m/%d/%Y')


def _determine_category(shipper: str) -> str:
    for shipper_key, category in SHIPPER_CATEGORY_MAP.items():
        if shipper_key in shipper or shipper in shipper_key:
            logger.debug("category shipper=%r matched key=%r -> %r",
                         shipper, shipper_key, category)
            return category
    logger.debug("category shipper=%r no match in %s",
                 shipper, list(SHIPPER_CATEGORY_MAP.keys()))
    return 'UNKNOWN CATEGORY - MANUAL REVIEW NEEDED'
