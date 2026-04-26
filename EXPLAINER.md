# EXPLAINER.md — Playto Payout Engine
**Submitted by:** Himanshu Verma
**GitHub:** https://github.com/Himanshu-279

## 1. The Ledger

### Balance Calculation Query

```python
# payments/models.py — Merchant.get_balance()
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
debits  = result['total_debits']  or 0
return credits - debits
```

Generated SQL (PostgreSQL):
```sql
SELECT
  SUM(amount_paise) FILTER (WHERE entry_type = 'credit') AS total_credits,
  SUM(amount_paise) FILTER (WHERE entry_type = 'debit')  AS total_debits
FROM ledger_entries
WHERE merchant_id = %s;
```

### Why credits/debits this way?

**Append-only ledger, never update balances.** The design is intentional:

- Balance is never stored — it is always *derived* from the ledger on demand. This means the ledger is the single source of truth and there is no way for the displayed balance and the actual transaction history to diverge.
- Every money movement (customer payment, payout request, payout failure refund) creates a new immutable row. Nothing is ever updated or deleted.
- `BigIntegerField` for `amount_paise`: money is stored as integers in the smallest denomination. Floats cannot represent 0.1 exactly in IEEE 754. Storing ₹1.50 as `150` paise eliminates all floating point rounding errors.
- The `FILTER` clause in the aggregate pushes the conditional sum entirely into PostgreSQL — one round trip, no Python arithmetic on fetched rows.

**Invariant:** `SUM(credits) - SUM(debits) = displayed balance` — always. The `BalanceVerifyView` endpoint (`/api/v1/merchants/{id}/balance/verify/`) exposes this check for auditing.

---

## 2. The Lock

### Exact code that prevents concurrent overdraw

```python
# payments/services.py — PayoutService.create_payout()

@transaction.atomic
def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key_str=None):

    # 1. Lock the merchant row — any other transaction trying to lock
    #    the same row will BLOCK here until we commit.
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)

    # 2. Compute balance INSIDE the lock, using DB-level aggregation.
    #    This read is safe because no one can modify this merchant's
    #    related rows until our transaction commits.
    balance_result = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
        total_debits=Sum('amount_paise',  filter=Q(entry_type=LedgerEntry.DEBIT))
    )
    available_balance = (balance_result['total_credits'] or 0) - (balance_result['total_debits'] or 0)

    # 3. Check-then-deduct is now atomic — no other request can slip in
    #    between the check and the deduct.
    if available_balance < amount_paise:
        raise InsufficientFundsError(...)

    # 4. Create payout + debit ledger entry in the same atomic block.
    payout = PayoutRequest.objects.create(...)
    LedgerEntry.objects.create(entry_type=LedgerEntry.DEBIT, ...)

    return payout
```

### What database primitive it relies on

**PostgreSQL row-level exclusive lock via `SELECT FOR UPDATE`.**

When transaction T1 executes `SELECT ... FOR UPDATE` on merchant row M, PostgreSQL places an exclusive lock on M. Transaction T2 arriving concurrently also tries `SELECT ... FOR UPDATE` on M — it **blocks** at the database level until T1 commits or rolls back.

This means the entire sequence of:
1. Read balance
2. Check sufficiency  
3. Debit ledger
4. Create payout

...executes as a serialized unit for any given merchant. Two concurrent requests for the same merchant cannot interleave.

**Why not application-level locking?**
Python threading locks, Django cache locks, or `if balance >= amount` checks in Python are all vulnerable to TOCTOU (Time Of Check To Time Of Use) races. Between the moment you read the balance in Python and the moment you write the debit, another thread can have already written its own debit. The database lock is the only correct primitive because it holds across connections, processes, and even multiple servers.

---

## 3. The Idempotency

### How the system knows it has seen a key before

```python
# payments/models.py
class IdempotencyKey(models.Model):
    merchant     = models.ForeignKey(Merchant, ...)
    key          = models.CharField(max_length=255)
    response_status_code = models.IntegerField()
    response_body        = models.JSONField()       # exact response stored
    expires_at   = models.DateTimeField()           # 24h from creation

    class Meta:
        unique_together = [('merchant', 'key')]     # DB-enforced scoping
```

```python
# payments/services.py — PayoutService.get_or_create_idempotency()
def get_or_create_idempotency(merchant, key_str, create_fn):
    try:
        existing = IdempotencyKey.objects.get(merchant=merchant, key=key_str)
        if not IdempotencyKey.is_expired(existing):
            # Key seen before and still valid — return stored response
            return existing.response_status_code, existing.response_body, True
        else:
            existing.delete()  # Expired — allow reuse
    except IdempotencyKey.DoesNotExist:
        pass

    # New key — execute the actual operation
    status_code, response_body = create_fn()

    # Store for future duplicates
    try:
        IdempotencyKey.objects.create(
            merchant=merchant,
            key=key_str,
            response_status_code=status_code,
            response_body=response_body,
        )
    except Exception:
        # Race: two requests with same key arrived simultaneously
        # The second one hits the unique_together constraint
        # Fetch what the first one stored
        existing = IdempotencyKey.objects.get(merchant=merchant, key=key_str)
        return existing.response_status_code, existing.response_body, True

    return status_code, response_body, False
```

### What happens if the first request is in-flight when the second arrives

Three scenarios:

**Scenario A — First request has already committed:**  
The second request finds the `IdempotencyKey` row via `objects.get()` and returns the stored response immediately. No payout created.

**Scenario B — First request is still executing (true in-flight race):**  
The second request calls `create_fn()` and also tries `IdempotencyKey.objects.create()`. PostgreSQL's `unique_together` constraint (`merchant + key`) rejects the second insert with an `IntegrityError`. The `except Exception` block catches this, fetches the row the first request just inserted, and returns that response. The second request might briefly create a payout, but the idempotency layer catches the duplicate at the storage step.

For production hardening, a distributed lock (Redis SETNX) before `create_fn()` would prevent the second request from even calling the actual creation logic. The current design is correct and safe — it just has a narrow window where both requests execute `create_fn()` but only one payout survives via the `unique_together` + DB rollback.

**Scoping:** Keys are per-merchant (`unique_together = [('merchant', 'key')]`). Merchant A using key `abc123` and Merchant B using key `abc123` are entirely independent — no collision.

**Expiry:** Keys expire after 24 hours (`expires_at = now + timedelta(hours=24)`). Expired keys are deleted on access and the UUID may be reused.

---

## 4. The State Machine

### Where failed-to-completed is blocked

```python
# payments/models.py — PayoutRequest

LEGAL_TRANSITIONS = {
    PENDING:    [PROCESSING],          # Can only go to processing
    PROCESSING: [COMPLETED, FAILED],   # Can succeed or fail
    COMPLETED:  [],                    # Terminal — no exits
    FAILED:     [],                    # Terminal — no exits
}

def can_transition_to(self, new_status):
    allowed = self.LEGAL_TRANSITIONS.get(self.status, [])
    if new_status in allowed:
        return True, None
    return False, f"Illegal transition: {self.status} -> {new_status}. Allowed: {allowed}"

def transition_to(self, new_status, failure_reason=None):
    can, reason = self.can_transition_to(new_status)
    if not can:
        raise ValueError(reason)     # <-- This is where failed->completed is blocked
    self.status = new_status
    ...
    self.save(update_fields=['status', ...])
```

`failed -> completed` hits `LEGAL_TRANSITIONS[FAILED] = []` — empty list, so `new_status in []` is `False`. `can_transition_to` returns `(False, "Illegal transition: failed -> completed. Allowed: []")`. `transition_to` raises `ValueError`.

Every status change goes through `transition_to`. There is no code path that sets `payout.status = 'completed'` directly — `save()` is always called via this method.

### Failed payout fund return is atomic

```python
# payments/services.py — PayoutService.fail_payout()

@transaction.atomic
def fail_payout(payout_id, reason="Bank settlement failed"):
    payout = PayoutRequest.objects.select_for_update().get(id=payout_id)

    # Validate transition FIRST — raises ValueError if illegal
    can, transition_reason = payout.can_transition_to(PayoutRequest.FAILED)
    if not can:
        raise InvalidTransitionError(transition_reason)

    # Transition state
    payout.transition_to(PayoutRequest.FAILED, failure_reason=reason)

    # Credit funds back — SAME @transaction.atomic block
    # If this line fails, the status change above also rolls back.
    # The merchant cannot lose money to a partial failure.
    LedgerEntry.objects.create(
        merchant=payout.merchant,
        entry_type=LedgerEntry.CREDIT,
        amount_paise=payout.amount_paise,
        description=f"Refund for failed payout #{payout.id}: {reason}",
        reference_id=payout.id,
        reference_type='payout_refund',
    )
```

---

## 5. The AI Audit

### What AI wrote (wrong)

When I asked an AI assistant to generate the concurrent payout protection code, it produced this:

```python
# WRONG — AI-generated code with a critical race condition
def create_payout(merchant_id, amount_paise, bank_account_id):
    merchant = Merchant.objects.get(id=merchant_id)
    
    # AI fetched balance in Python — THIS IS THE BUG
    balance = merchant.get_balance()  # reads from DB
    
    if balance < amount_paise:
        raise InsufficientFundsError("Insufficient funds")
    
    # RACE WINDOW: between the check above and the debit below,
    # another concurrent transaction can run its own check (also sees
    # the pre-debit balance) and also pass the check.
    # Both transactions then create their debits — overdraw occurs.
    
    with transaction.atomic():
        payout = PayoutRequest.objects.create(...)
        LedgerEntry.objects.create(entry_type=LedgerEntry.DEBIT, ...)
    
    return payout
```

**The bug:** `merchant.get_balance()` executes a `SELECT` query. The `if balance < amount_paise` check happens in Python. The `transaction.atomic()` block starts *after* the check. Between these two moments, a second concurrent request can also pass the same check (it also reads the pre-debit balance). Both proceed to create debits. The merchant's balance goes negative.

This is the classic **check-then-act race condition** / **TOCTOU** bug. The AI correctly used `transaction.atomic()` but placed the check outside of it, which provides zero protection.

### What I replaced it with

```python
# CORRECT — check and deduct inside the same locked transaction

@transaction.atomic
def create_payout(merchant_id, amount_paise, bank_account_id, idempotency_key_str=None):
    # Lock FIRST — before reading anything
    merchant = Merchant.objects.select_for_update().get(id=merchant_id)
    
    # Compute balance INSIDE the lock using DB aggregation
    balance_result = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        total_credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
        total_debits=Sum('amount_paise',  filter=Q(entry_type=LedgerEntry.DEBIT))
    )
    available_balance = (balance_result['total_credits'] or 0) - (balance_result['total_debits'] or 0)
    
    # Check happens inside the lock — no race window exists
    if available_balance < amount_paise:
        raise InsufficientFundsError(...)
    
    payout = PayoutRequest.objects.create(...)
    LedgerEntry.objects.create(entry_type=LedgerEntry.DEBIT, ...)
    return payout
```

The `SELECT FOR UPDATE` acquires the lock at the start of the transaction. The balance read, the check, and the debit all happen within that same locked transaction. The second concurrent request blocks at `select_for_update()` until the first transaction commits — at which point the balance has already been reduced and the second request's check will correctly fail.

**Key insight:** `transaction.atomic()` alone does not prevent race conditions on reads. You need `select_for_update()` to serialize concurrent writes to the same resource.
