"""Ledger adapter interface (spec section 6). Every operation is idempotent:
book_payment must call find_booking first and never create a second entry."""
from abc import ABC, abstractmethod


class Refused(Exception):
    """The adapter cannot book the event exactly. It reports; it never repairs."""


class LedgerAdapter(ABC):
    name = "abstract"

    @abstractmethod
    def find_booking(self, event_key):
        """Existing ledger reference for an event key, or None."""

    @abstractmethod
    def find_bills(self, po_id):
        """Open bills linked to the PO whose reference hash is po_id."""

    @abstractmethod
    def book_payment(self, event, order, bill):
        """Book a Paid event against a bill. Returns the ledger reference."""

    def book_refund(self, event, order):
        raise Refused("refunds are not supported by this adapter")

    @abstractmethod
    def open_bills(self, since):
        """Bills with an unpaid balance, for the omission report."""
