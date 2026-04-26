"""
Tests for Playto Payout Engine.
Covers the two most critical requirements:
1. Concurrency — concurrent payout requests must not overdraw balance
2. Idempotency — same key must return identical response, no duplicate payout
"""
import uuid
import threading
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient
from unittest.mock import patch

from .models import Merchant, BankAccount, LedgerEntry, PayoutRequest, IdempotencyKey
from .services import PayoutService, InsufficientFundsError
from .services import LedgerService


def create_test_merchant(name="Test Merchant", email=None, balance_paise=10000):
    """Helper to create a merchant with a given balance."""
    email = email or f"{uuid.uuid4().hex[:8]}@test.com"
    merchant = Merchant.objects.create(
        name=name,
        email=email,
        bank_account_number="1234567890",
        bank_ifsc="HDFC0001234",
        bank_account_holder=name,
    )
    bank_account = BankAccount.objects.create(
        merchant=merchant,
        account_number="1234567890",
        ifsc_code="HDFC0001234",
        account_holder_name=name,
        is_primary=True,
    )
    if balance_paise > 0:
        LedgerService.credit_merchant(
            merchant=merchant,
            amount_paise=balance_paise,
            description="Test credit",
        )
    return merchant, bank_account


class ConcurrencyTest(TransactionTestCase):
    """
    TransactionTestCase is REQUIRED here (not TestCase) because:
    - TestCase wraps everything in a single transaction that never commits
    - SELECT FOR UPDATE needs real concurrent transactions to test properly
    - TransactionTestCase actually commits to DB between tests
    """

    def test_concurrent_payouts_no_overdraw(self):
        """
        Two simultaneous 6000 paise (Rs.60) payout requests when balance = 10000 paise (Rs.100).
        EXACTLY ONE should succeed. The other must fail with InsufficientFundsError.
        This tests that SELECT FOR UPDATE serializes concurrent requests correctly.
        """
        merchant, bank_account = create_test_merchant(balance_paise=10000)

        results = []
        errors = []

        def attempt_payout():
            try:
                payout = PayoutService.create_payout(
                    merchant_id=str(merchant.id),
                    amount_paise=6000,
                    bank_account_id=str(bank_account.id),
                    idempotency_key_str=str(uuid.uuid4()),
                )
                results.append(('success', payout.id))
            except InsufficientFundsError as e:
                results.append(('insufficient', str(e)))
            except Exception as e:
                errors.append(str(e))

        # Launch 2 concurrent threads
        threads = [threading.Thread(target=attempt_payout) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No unexpected errors
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Exactly one success, one failure
        successes = [r for r in results if r[0] == 'success']
        failures = [r for r in results if r[0] == 'insufficient']

        self.assertEqual(len(successes), 1, f"Expected 1 success, got {len(successes)}. Results: {results}")
        self.assertEqual(len(failures), 1, f"Expected 1 failure, got {len(failures)}. Results: {results}")

        # Verify final balance is correct: 10000 - 6000 = 4000 paise (held as debit)
        # The successful payout debited 6000, so ledger should show 10000 - 6000 = 4000
        final_balance = merchant.get_balance()
        self.assertEqual(
            final_balance, 4000,
            f"Balance should be 4000 paise after one 6000 debit, got {final_balance}"
        )

    def test_five_concurrent_requests_only_one_succeeds(self):
        """
        Five simultaneous requests for 7000 paise when balance = 10000 paise.
        Only one can succeed — there's not enough for two.
        """
        merchant, bank_account = create_test_merchant(balance_paise=10000)
        results = []

        def attempt():
            try:
                payout = PayoutService.create_payout(
                    merchant_id=str(merchant.id),
                    amount_paise=7000,
                    bank_account_id=str(bank_account.id),
                    idempotency_key_str=str(uuid.uuid4()),
                )
                results.append('success')
            except InsufficientFundsError:
                results.append('insufficient')

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = results.count('success')
        self.assertEqual(successes, 1, f"Expected exactly 1 success, got {successes}. All: {results}")

        # Balance after one 7000 debit = 3000
        final_balance = merchant.get_balance()
        self.assertEqual(final_balance, 3000)

    def test_ledger_invariant_after_concurrent_requests(self):
        """
        After concurrent requests, ledger integrity must hold:
        sum(credits) - sum(debits) == get_balance()
        """
        merchant, bank_account = create_test_merchant(balance_paise=50000)
        results = []

        def attempt():
            try:
                PayoutService.create_payout(
                    merchant_id=str(merchant.id),
                    amount_paise=20000,
                    bank_account_id=str(bank_account.id),
                    idempotency_key_str=str(uuid.uuid4()),
                )
                results.append('success')
            except InsufficientFundsError:
                results.append('insufficient')

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify invariant
        from django.db.models import Sum, Q
        agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
            credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
            debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT)),
        )
        credits = agg['credits'] or 0
        debits = agg['debits'] or 0
        derived = credits - debits
        displayed = merchant.get_balance()

        self.assertEqual(
            derived, displayed,
            f"Ledger invariant broken! Derived: {derived}, Displayed: {displayed}"
        )
        self.assertGreaterEqual(displayed, 0, "Balance went negative!")


class IdempotencyTest(TestCase):
    """Tests for idempotency key behavior."""

    def setUp(self):
        self.merchant, self.bank_account = create_test_merchant(balance_paise=100000)
        self.client = APIClient()

    def test_same_key_returns_same_response(self):
        """
        Two POST requests with identical Idempotency-Key must:
        1. Return the exact same response body
        2. Create only ONE payout in the database
        """
        idempotency_key = str(uuid.uuid4())

        payload = {
            "amount_paise": 5000,
            "bank_account_id": str(self.bank_account.id),
            "merchant_id": str(self.merchant.id),
        }

        response1 = self.client.post(
            '/api/v1/payouts/',
            data=payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
            HTTP_X_MERCHANT_ID=str(self.merchant.id),
        )
        response2 = self.client.post(
            '/api/v1/payouts/',
            data=payload,
            format='json',
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
            HTTP_X_MERCHANT_ID=str(self.merchant.id),
        )

        self.assertEqual(response1.status_code, 201)
        self.assertEqual(response2.status_code, 201)

        # Same payout ID in both responses
        self.assertEqual(response1.data['id'], response2.data['id'])

        # Second response should have replay header
        self.assertEqual(response2['X-Idempotent-Replayed'], 'true')

        # Only ONE payout created
        payout_count = PayoutRequest.objects.filter(
            merchant=self.merchant,
            idempotency_key=idempotency_key,
        ).count()
        self.assertEqual(payout_count, 1, f"Expected 1 payout, got {payout_count}")

        # Only ONE debit in ledger
        debit_count = LedgerEntry.objects.filter(
            merchant=self.merchant,
            entry_type=LedgerEntry.DEBIT,
        ).count()
        self.assertEqual(debit_count, 1, f"Expected 1 debit ledger entry, got {debit_count}")

    def test_different_keys_create_different_payouts(self):
        """Different idempotency keys must create separate payouts."""
        key1 = str(uuid.uuid4())
        key2 = str(uuid.uuid4())

        payload = {
            "amount_paise": 5000,
            "bank_account_id": str(self.bank_account.id),
            "merchant_id": str(self.merchant.id),
        }

        r1 = self.client.post('/api/v1/payouts/', data=payload, format='json',
                              HTTP_IDEMPOTENCY_KEY=key1,
                              HTTP_X_MERCHANT_ID=str(self.merchant.id))
        r2 = self.client.post('/api/v1/payouts/', data=payload, format='json',
                              HTTP_IDEMPOTENCY_KEY=key2,
                              HTTP_X_MERCHANT_ID=str(self.merchant.id))

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['id'], r2.data['id'])

    def test_idempotency_key_scoped_per_merchant(self):
        """Same key from different merchants creates separate payouts."""
        merchant2, bank2 = create_test_merchant(
            name="Another Merchant",
            balance_paise=100000,
        )

        shared_key = str(uuid.uuid4())
        payload = {"amount_paise": 5000}

        r1 = self.client.post(
            '/api/v1/payouts/',
            data={**payload, "bank_account_id": str(self.bank_account.id)},
            format='json',
            HTTP_IDEMPOTENCY_KEY=shared_key,
            HTTP_X_MERCHANT_ID=str(self.merchant.id),
        )
        r2 = self.client.post(
            '/api/v1/payouts/',
            data={**payload, "bank_account_id": str(bank2.id)},
            format='json',
            HTTP_IDEMPOTENCY_KEY=shared_key,
            HTTP_X_MERCHANT_ID=str(merchant2.id),
        )

        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        # Different merchants — different payouts even with same key
        self.assertNotEqual(r1.data['id'], r2.data['id'])

    def test_missing_idempotency_key_returns_400(self):
        """Requests without Idempotency-Key must be rejected."""
        r = self.client.post(
            '/api/v1/payouts/',
            data={"amount_paise": 5000, "bank_account_id": str(self.bank_account.id)},
            format='json',
            HTTP_X_MERCHANT_ID=str(self.merchant.id),
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.data)


class StateMachineTest(TestCase):
    """Tests for payout state machine enforcement."""

    def setUp(self):
        self.merchant, self.bank_account = create_test_merchant(balance_paise=50000)

    def test_legal_transitions(self):
        """pending -> processing -> completed is legal."""
        payout = PayoutService.create_payout(
            merchant_id=str(self.merchant.id),
            amount_paise=1000,
            bank_account_id=str(self.bank_account.id),
        )
        self.assertEqual(payout.status, PayoutRequest.PENDING)

        PayoutService.process_payout(str(payout.id))
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutRequest.PROCESSING)

        PayoutService.complete_payout(str(payout.id))
        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutRequest.COMPLETED)

    def test_illegal_transition_completed_to_pending(self):
        """completed -> pending must be rejected."""
        payout = PayoutService.create_payout(
            merchant_id=str(self.merchant.id),
            amount_paise=1000,
            bank_account_id=str(self.bank_account.id),
        )
        PayoutService.process_payout(str(payout.id))
        PayoutService.complete_payout(str(payout.id))

        payout.refresh_from_db()
        self.assertEqual(payout.status, PayoutRequest.COMPLETED)

        can, reason = payout.can_transition_to(PayoutRequest.PENDING)
        self.assertFalse(can)

    def test_failed_payout_refunds_atomically(self):
        """On failure, funds must be returned atomically."""
        initial_balance = self.merchant.get_balance()

        payout = PayoutService.create_payout(
            merchant_id=str(self.merchant.id),
            amount_paise=5000,
            bank_account_id=str(self.bank_account.id),
        )

        # Balance reduced after payout creation
        after_create = self.merchant.get_balance()
        self.assertEqual(after_create, initial_balance - 5000)

        PayoutService.process_payout(str(payout.id))
        PayoutService.fail_payout(str(payout.id), reason="Test failure")

        # Balance restored after failure
        after_fail = self.merchant.get_balance()
        self.assertEqual(after_fail, initial_balance)

        # Refund entry exists in ledger
        refund = LedgerEntry.objects.filter(
            merchant=self.merchant,
            entry_type=LedgerEntry.CREDIT,
            reference_id=payout.id,
        ).first()
        self.assertIsNotNone(refund)
        self.assertEqual(refund.amount_paise, 5000)


class BalanceIntegrityTest(TestCase):
    """Tests for money integrity — ledger invariant."""

    def test_balance_never_stored_always_derived(self):
        """Balance must equal sum(credits) - sum(debits) at all times."""
        from django.db.models import Sum, Q

        merchant, bank = create_test_merchant(balance_paise=20000)

        # Create a payout
        payout = PayoutService.create_payout(
            merchant_id=str(merchant.id),
            amount_paise=5000,
            bank_account_id=str(bank.id),
        )

        # Fail it to trigger refund
        PayoutService.process_payout(str(payout.id))
        PayoutService.fail_payout(str(payout.id))

        # Verify invariant
        agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
            credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
            debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT)),
        )
        derived = (agg['credits'] or 0) - (agg['debits'] or 0)
        displayed = merchant.get_balance()

        self.assertEqual(derived, displayed)
        # Balance should be back to 20000 after refund
        self.assertEqual(displayed, 20000)
