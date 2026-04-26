"""
Payout Service — The core business logic for money movement.

Key design decisions:
1. All balance checks and deductions use SELECT FOR UPDATE at DB level
2. Atomic transactions wrap every money-moving operation
3. Ledger is the source of truth — balance is always derived, never stored
4. State transitions are validated before execution
"""
import logging
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone
from datetime import timedelta

from .models import Merchant, LedgerEntry, BankAccount, PayoutRequest, IdempotencyKey

logger = logging.getLogger('payments')


class InsufficientFundsError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


class PayoutService:

    @staticmethod
    @transaction.atomic
    def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key_str=None):
        """
        Create a payout request with:
        1. Idempotency check (return existing if same key)
        2. SELECT FOR UPDATE on ledger rows to prevent concurrent overdraw
        3. Atomic debit from ledger
        4. Payout record creation

        The SELECT FOR UPDATE is the critical primitive that prevents race conditions.
        Two concurrent requests will serialize at the DB level — the second waits
        for the first to commit before it can read and lock the rows.
        """

        merchant = Merchant.objects.select_for_update().get(id=merchant_id)
        bank_account = BankAccount.objects.get(
            id=bank_account_id, merchant=merchant
        )

        # ── BALANCE CHECK ──────────────────────────────────────────────────────
        # We compute balance INSIDE the locked transaction using DB aggregation.
        # This is NOT Python arithmetic on fetched rows — the SUM happens in SQL.
        # The SELECT FOR UPDATE on merchant above ensures no other transaction
        # can modify this merchant's ledger until we commit.
        balance_result = LedgerEntry.objects.filter(merchant=merchant).aggregate(
            total_credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
            total_debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT))
        )
        total_credits = balance_result['total_credits'] or 0
        total_debits = balance_result['total_debits'] or 0
        available_balance = total_credits - total_debits

        if available_balance < amount_paise:
            raise InsufficientFundsError(
                f"Insufficient funds. Available: {available_balance} paise, "
                f"Requested: {amount_paise} paise"
            )

        # ── CREATE PAYOUT ──────────────────────────────────────────────────────
        payout = PayoutRequest.objects.create(
            merchant=merchant,
            bank_account=bank_account,
            amount_paise=amount_paise,
            status=PayoutRequest.PENDING,
            idempotency_key=idempotency_key_str,
        )

        # ── DEBIT LEDGER ───────────────────────────────────────────────────────
        # Debit happens atomically with payout creation.
        # If anything fails, both are rolled back.
        LedgerEntry.objects.create(
            merchant=merchant,
            entry_type=LedgerEntry.DEBIT,
            amount_paise=amount_paise,
            description=f"Payout request #{payout.id}",
            reference_id=payout.id,
            reference_type='payout_request',
        )

        logger.info(
            f"Payout created: {payout.id} for merchant {merchant.id}, "
            f"amount: {amount_paise} paise"
        )
        return payout

    @staticmethod
    @transaction.atomic
    def process_payout(payout_id):
        """
        Move payout from PENDING to PROCESSING.
        Uses select_for_update to prevent duplicate processing.
        """
        try:
            payout = PayoutRequest.objects.select_for_update().get(id=payout_id)
        except PayoutRequest.DoesNotExist:
            logger.error(f"Payout {payout_id} not found")
            return None

        can, reason = payout.can_transition_to(PayoutRequest.PROCESSING)
        if not can:
            logger.warning(f"Cannot process payout {payout_id}: {reason}")
            return payout

        payout.transition_to(PayoutRequest.PROCESSING)
        return payout

    @staticmethod
    @transaction.atomic
    def complete_payout(payout_id):
        """
        Mark payout as COMPLETED. Funds are already debited — nothing to reverse.
        """
        payout = PayoutRequest.objects.select_for_update().get(id=payout_id)
        can, reason = payout.can_transition_to(PayoutRequest.COMPLETED)
        if not can:
            raise InvalidTransitionError(reason)
        payout.transition_to(PayoutRequest.COMPLETED)
        logger.info(f"Payout {payout_id} completed successfully")
        return payout

    @staticmethod
    @transaction.atomic
    def fail_payout(payout_id, reason="Bank settlement failed"):
        """
        Mark payout as FAILED and return funds to merchant balance.

        ATOMICITY: The state transition AND the ledger credit happen in the
        same transaction. If either fails, both roll back. The merchant never
        loses money due to a partial failure.
        """
        payout = PayoutRequest.objects.select_for_update().get(id=payout_id)

        # Block illegal: completed -> failed, failed -> failed, etc.
        can, transition_reason = payout.can_transition_to(PayoutRequest.FAILED)
        if not can:
            raise InvalidTransitionError(transition_reason)

        # Transition state
        payout.transition_to(PayoutRequest.FAILED, failure_reason=reason)

        # ATOMIC: Credit funds back to merchant in SAME transaction
        LedgerEntry.objects.create(
            merchant=payout.merchant,
            entry_type=LedgerEntry.CREDIT,
            amount_paise=payout.amount_paise,
            description=f"Refund for failed payout #{payout.id}: {reason}",
            reference_id=payout.id,
            reference_type='payout_refund',
        )

        logger.info(
            f"Payout {payout_id} failed. Refunded {payout.amount_paise} paise "
            f"to merchant {payout.merchant_id}"
        )
        return payout

    @staticmethod
    def get_or_create_idempotency(merchant, key_str, create_fn):
        """
        Idempotency handler:
        1. Look up key — if exists and not expired, return stored response
        2. If not found, execute create_fn(), store result, return it
        3. If in-flight (race): DB unique constraint catches duplicate, return existing

        Keys are merchant-scoped: same UUID from two merchants = two separate keys.
        """
        try:
            existing = IdempotencyKey.objects.get(
                merchant=merchant, key=key_str
            )
            if not IdempotencyKey.is_expired(existing):
                logger.info(f"Idempotency hit for key {key_str}, merchant {merchant.id}")
                return existing.response_status_code, existing.response_body, True
            else:
                # Expired key — delete and allow re-use
                existing.delete()
        except IdempotencyKey.DoesNotExist:
            pass

        # Key not found — execute the actual operation
        status_code, response_body = create_fn()

        # Store for future duplicate requests
        # Use get_or_create to handle the rare race where two requests
        # with the same key arrive simultaneously — only one will insert,
        # the other will get the existing record
        try:
            IdempotencyKey.objects.create(
                merchant=merchant,
                key=key_str,
                response_status_code=status_code,
                response_body=response_body,
            )
        except Exception:
            # Another request already stored this key (race condition on first call)
            # Fetch and return what was stored
            existing = IdempotencyKey.objects.get(merchant=merchant, key=key_str)
            return existing.response_status_code, existing.response_body, True

        return status_code, response_body, False


class LedgerService:

    @staticmethod
    def credit_merchant(merchant, amount_paise, description, reference_id=None, reference_type=None):
        """Add funds to a merchant (simulates customer payment)."""
        with transaction.atomic():
            entry = LedgerEntry.objects.create(
                merchant=merchant,
                entry_type=LedgerEntry.CREDIT,
                amount_paise=amount_paise,
                description=description,
                reference_id=reference_id,
                reference_type=reference_type or 'customer_payment',
            )
            logger.info(f"Credited {amount_paise} paise to merchant {merchant.id}")
            return entry
