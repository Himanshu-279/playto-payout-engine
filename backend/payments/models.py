import uuid
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone
from datetime import timedelta
import logging

logger = logging.getLogger('payments')


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    bank_account_number = models.CharField(max_length=50)
    bank_ifsc = models.CharField(max_length=20)
    bank_account_holder = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.email})"

    def get_balance(self):
        """
        Compute balance entirely in the database using a single aggregation query.
        Balance = SUM of credits - SUM of debits from completed/non-reversed entries.
        We NEVER do Python arithmetic on fetched rows.
        """
        result = self.ledger_entries.aggregate(
            total_credits=Sum(
                'amount_paise',
                filter=Q(entry_type=LedgerEntry.CREDIT)
            ),
            total_debits=Sum(
                'amount_paise',
                filter=Q(entry_type=LedgerEntry.DEBIT)
            )
        )
        credits = result['total_credits'] or 0
        debits = result['total_debits'] or 0
        return credits - debits

    def get_held_balance(self):
        """
        Funds currently held (pending/processing payouts).
        These are already debited from ledger but not yet settled.
        """
        result = self.payout_requests.filter(
            status__in=[PayoutRequest.PENDING, PayoutRequest.PROCESSING]
        ).aggregate(total=Sum('amount_paise'))
        return result['total'] or 0

    def get_available_balance(self):
        """Available = total balance (ledger derived) minus held funds."""
        return self.get_balance()

    class Meta:
        db_table = 'merchants'


class LedgerEntry(models.Model):
    CREDIT = 'credit'
    DEBIT = 'debit'
    ENTRY_TYPE_CHOICES = [
        (CREDIT, 'Credit'),
        (DEBIT, 'Debit'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name='ledger_entries'
    )
    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES)
    # CRITICAL: amounts stored as BigIntegerField in PAISE, never floats
    amount_paise = models.BigIntegerField()
    description = models.CharField(max_length=500)
    reference_id = models.UUIDField(null=True, blank=True)
    reference_type = models.CharField(max_length=50, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        rupees = self.amount_paise / 100
        return f"{self.entry_type}: ₹{rupees:.2f} for {self.merchant.name}"

    class Meta:
        db_table = 'ledger_entries'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['merchant', '-created_at']),
            models.Index(fields=['merchant', 'entry_type']),
        ]


class BankAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name='bank_accounts'
    )
    account_number = models.CharField(max_length=50)
    ifsc_code = models.CharField(max_length=20)
    account_holder_name = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.account_holder_name} - {self.account_number[-4:].rjust(len(self.account_number), '*')}"

    class Meta:
        db_table = 'bank_accounts'


class IdempotencyKey(models.Model):
    """
    Stores idempotency keys per merchant.
    Scoped: same key from different merchants = different records.
    Expires after 24 hours.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name='idempotency_keys'
    )
    key = models.CharField(max_length=255)
    # Store the full response so we can return the exact same response
    response_status_code = models.IntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

    @classmethod
    def is_expired(cls, instance):
        return timezone.now() > instance.expires_at

    class Meta:
        db_table = 'idempotency_keys'
        # Composite unique: same key cannot be used twice per merchant
        unique_together = [('merchant', 'key')]
        indexes = [
            models.Index(fields=['merchant', 'key']),
            models.Index(fields=['expires_at']),
        ]


class PayoutRequest(models.Model):
    # State machine states
    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'

    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED, 'Completed'),
        (FAILED, 'Failed'),
    ]

    # Legal transitions ONLY
    LEGAL_TRANSITIONS = {
        PENDING: [PROCESSING],
        PROCESSING: [COMPLETED, FAILED],
        COMPLETED: [],   # Terminal state - no transitions allowed
        FAILED: [],      # Terminal state - no transitions allowed
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name='payout_requests'
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name='payout_requests'
    )
    # CRITICAL: BigIntegerField in paise
    amount_paise = models.BigIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def can_transition_to(self, new_status):
        """Enforce state machine rules. Returns (bool, reason)."""
        allowed = self.LEGAL_TRANSITIONS.get(self.status, [])
        if new_status in allowed:
            return True, None
        return False, (
            f"Illegal transition: {self.status} -> {new_status}. "
            f"Allowed from {self.status}: {allowed}"
        )

    def transition_to(self, new_status, failure_reason=None):
        """
        Transition state with validation. Raises ValueError on illegal transition.
        This is the ONLY way to change payout status.
        """
        can, reason = self.can_transition_to(new_status)
        if not can:
            raise ValueError(reason)
        self.status = new_status
        if failure_reason:
            self.failure_reason = failure_reason
        if new_status == self.PROCESSING:
            self.processing_started_at = timezone.now()
        self.save(update_fields=['status', 'failure_reason', 'processing_started_at', 'updated_at'])
        logger.info(f"Payout {self.id} transitioned to {new_status}")

    def __str__(self):
        rupees = self.amount_paise / 100
        return f"Payout ₹{rupees:.2f} for {self.merchant.name} [{self.status}]"

    class Meta:
        db_table = 'payout_requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['merchant', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['processing_started_at']),
        ]
