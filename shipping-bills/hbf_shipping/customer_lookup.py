"""
Customer lookup and validation module.
Validates invoice consignees against the HBF customer master list.

Match tiers (tried in order):
    1. Exact
    2. Case-insensitive
    3. Normalized — lowercase, punctuation stripped, whitespace collapsed.
       Catches benign differences like "VICTORVILLE FCI 1" vs "Victorville - FCI 1".
    4. Fuzzy — difflib.SequenceMatcher ratio ≥ FUZZY_THRESHOLD on normalized forms.
       Catches OCR typos and minor spelling drift while avoiding the false-positive
       risk of substring matching (e.g. "FCI" wrongly matching "Tallahassee FCI").
"""

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Union

import xlrd


logger = logging.getLogger(__name__)


FUZZY_THRESHOLD = 0.88  # min SequenceMatcher ratio on normalized strings

# Anchor the default to project_root/data/ so the lookup works regardless
# of the caller's CWD. project_root = parent of this package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CUSTOMER_FILE = _PROJECT_ROOT / 'data' / 'hbf-customers.xls'


def _normalize(name: str) -> str:
    """Lowercase, replace non-alphanumerics with spaces, collapse whitespace."""
    s = re.sub(r'[^a-z0-9]+', ' ', name.lower())
    return re.sub(r'\s+', ' ', s).strip()


class CustomerValidator:
    """Validates customers against the HBF customer master list."""

    def __init__(self, customer_file: Union[str, Path, None] = None):
        self.customer_file = Path(customer_file) if customer_file else DEFAULT_CUSTOMER_FILE
        self.customers = self._load_customers()
        # Normalized form → canonical (original) name. First occurrence wins
        # on collisions, which is fine because exact/case-insensitive tiers
        # run first anyway.
        self._by_normal = {_normalize(c): c for c in self.customers}

    def _load_customers(self):
        wb = xlrd.open_workbook(str(self.customer_file))
        sheet = wb.sheet_by_index(0)
        customers = set()
        for row in range(1, sheet.nrows):
            customer_name = sheet.cell_value(row, 0)
            if customer_name:
                customers.add(str(customer_name).strip())
        return customers

    def validate_customer(self, consignee: str) -> Dict[str, any]:
        """Validate a consignee against the customer list. See module docstring
        for the match tiers. Returns a dict with is_valid, is_distributor,
        matched_name, and a message describing which tier matched.
        """
        if not consignee:
            logger.debug("validate_customer: empty consignee")
            return self._miss('', 'No consignee name provided', distributor=False)

        consignee_s = consignee.strip()
        logger.debug("validate_customer: looking up %r against %d customers",
                     consignee_s, len(self.customers))

        # 1. Exact
        if consignee_s in self.customers:
            logger.debug("match tier=exact -> %r", consignee_s)
            return self._hit(consignee_s, f'Customer found: {consignee_s}')

        # 2. Case-insensitive
        lower = consignee_s.lower()
        for customer in self.customers:
            if customer.lower() == lower:
                logger.debug("match tier=case-insensitive -> %r", customer)
                return self._hit(customer, f'Customer found (case-insensitive): {customer}')

        # 3. Normalized (punctuation + whitespace)
        norm = _normalize(consignee_s)
        if norm in self._by_normal:
            matched = self._by_normal[norm]
            logger.debug("match tier=normalized -> %r (norm=%r)", matched, norm)
            return self._hit(matched, f'Customer found (normalized): {matched}')

        # 4. Fuzzy (difflib ratio on normalized forms)
        best_name, best_ratio = None, 0.0
        for cand_norm, cand_name in self._by_normal.items():
            r = SequenceMatcher(None, norm, cand_norm).ratio()
            if r > best_ratio:
                best_ratio, best_name = r, cand_name
        if best_ratio >= FUZZY_THRESHOLD:
            logger.debug("match tier=fuzzy ratio=%.3f -> %r (threshold=%.2f)",
                         best_ratio, best_name, FUZZY_THRESHOLD)
            return self._hit(
                best_name,
                f'Customer found (fuzzy {best_ratio:.2f}): {best_name}',
            )

        logger.debug("no match (best fuzzy ratio=%.3f for %r) — distributor case",
                     best_ratio, best_name)
        return self._miss(consignee_s,
                          f'Customer not found - distributor case: {consignee_s}')

    def _hit(self, name, message):
        return {'is_valid': True, 'is_distributor': False,
                'matched_name': name, 'message': message}

    def _miss(self, name, message, distributor=True):
        return {'is_valid': False, 'is_distributor': distributor,
                'matched_name': None, 'message': message}

    def get_customer_count(self):
        """Return the number of customers loaded."""
        return len(self.customers)


if __name__ == '__main__':
    # Test the customer validator
    validator = CustomerValidator()

    print(f"Loaded {validator.get_customer_count()} customers\n")

    # Test cases
    test_customers = [
        'Gold Star Foods',  # From example invoice
        'A.F. Wendling\'s Food Service',  # From customer list
        'Non-Existent Customer',  # Should be flagged as distributor
    ]

    for test_customer in test_customers:
        result = validator.validate_customer(test_customer)
        print(f"Testing: {test_customer}")
        print(f"  Valid: {result['is_valid']}")
        print(f"  Distributor: {result['is_distributor']}")
        print(f"  Matched: {result['matched_name']}")
        print(f"  Message: {result['message']}")
        print()
