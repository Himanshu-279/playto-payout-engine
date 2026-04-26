# Playto Payout Engine

**Submitted by:** Himanshu Verma  
**GitHub:** https://github.com/Himanshu-279  
**Role:** Founding Engineer Challenge — 2026

Cross-border payout infrastructure for Indian merchants. Merchants accumulate balance from international customer payments (USD) and withdraw to their Indian bank accounts (INR).

## Architecture

```
Django + DRF  ──► PostgreSQL (ledger source of truth)
     │
     └──► Celery Worker  ──► Redis (broker)
               │
               └──► Celery Beat (periodic stuck-payout retries)

React + Tailwind  ──►  REST API  (live status polling every 3s)
```

## Quick Start (Docker — Recommended)

```bash
git clone <repo-url>
cd playto-payout
docker-compose up --build
```

- **Frontend:** http://localhost:3000
- **Backend API:** http://localhost:8000
- **Admin:** http://localhost:8000/admin

Database is seeded automatically with 3 merchants and credit history.

---

## Manual Setup (Local Development)

### Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Redis 7+
- Node.js 18+

### Backend

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your DB credentials

# Create database
createdb playto_payout   # or via psql: CREATE DATABASE playto_payout;

# Run migrations
python manage.py migrate

# Seed test data (3 merchants with balance)
python manage.py seed_data

# Start Django server
python manage.py runserver
```

### Celery Workers (separate terminals)

```bash
# Terminal 2 — Worker
celery -A config worker --loglevel=info

# Terminal 3 — Beat scheduler (periodic retry of stuck payouts)
celery -A config beat --loglevel=info
```

### Frontend

```bash
cd frontend
npm install
npm start    # Opens http://localhost:3000
```

---

## Running Tests

```bash
cd backend

# All tests
python manage.py test payments

# Specific test classes
python manage.py test payments.tests.ConcurrencyTest
python manage.py test payments.tests.IdempotencyTest
python manage.py test payments.tests.StateMachineTest
python manage.py test payments.tests.BalanceIntegrityTest
```

**Note on concurrency tests:** `ConcurrencyTest` uses `TransactionTestCase` (not `TestCase`) because it needs real DB commits to test SELECT FOR UPDATE behavior. These tests require a live PostgreSQL connection — they cannot run with SQLite.

---

## API Reference

### Merchants

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/merchants/` | GET | List all merchants |
| `/api/v1/merchants/{id}/` | GET | Dashboard: balance, ledger, payouts |
| `/api/v1/merchants/{id}/balance/verify/` | GET | Audit: verify ledger invariant |

### Payouts

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/payouts/` | POST | Create payout request |
| `/api/v1/payouts/` | GET | List payouts (pass `?merchant_id=`) |
| `/api/v1/payouts/{id}/` | GET | Payout detail |
| `/api/v1/payouts/{id}/status/` | GET | Lightweight status poll |

### Required Headers for POST /api/v1/payouts/

```
Idempotency-Key: <uuid>        # Required. Merchant-scoped, 24h expiry
X-Merchant-ID: <merchant-uuid> # Required
Content-Type: application/json
```

### Request Body

```json
{
  "amount_paise": 50000,
  "bank_account_id": "<uuid>"
}
```

### Example cURL

```bash
# Get merchants and their IDs
curl http://localhost:8000/api/v1/merchants/

# Request a payout
curl -X POST http://localhost:8000/api/v1/payouts/ \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "X-Merchant-ID: <merchant-uuid>" \
  -d '{"amount_paise": 50000, "bank_account_id": "<bank-account-uuid>"}'

# Check balance integrity
curl http://localhost:8000/api/v1/merchants/<id>/balance/verify/
```

---

## Technical Design

### Money Integrity
- All amounts stored as `BigIntegerField` in **paise** (1 rupee = 100 paise). Never floats.
- Balance is **never stored** — always derived: `SUM(credits) - SUM(debits)` via SQL aggregation.
- Every money movement creates an immutable ledger entry.

### Concurrency
- `SELECT FOR UPDATE` on the merchant row serializes concurrent payout requests at the DB level.
- The balance check and ledger debit happen within the same locked `@transaction.atomic` block.
- Tested with `threading.Thread` in `ConcurrencyTest` — two simultaneous 60-rupee requests on a 100-rupee balance: exactly one succeeds.

### Idempotency
- `Idempotency-Key` header (UUID) required on all payout creation requests.
- Keys stored with full response body. Second call returns stored response without creating a new payout.
- Keys scoped per merchant via `unique_together = [('merchant', 'key')]`.
- Keys expire after 24 hours.

### State Machine
```
PENDING → PROCESSING → COMPLETED
                    ↘ FAILED (funds returned atomically)
```
Illegal transitions raise `ValueError`. Completed and failed are terminal states.

### Retry Logic
- Celery Beat runs `retry_stuck_payouts` every 30 seconds.
- Payouts stuck in `PROCESSING` for >30 seconds are re-queued.
- Exponential backoff: `5s, 10s, 20s`. Max 3 attempts, then `FAILED` + refund.

### Payout Simulation
- 70% success → `completed`
- 20% failure → `failed` (funds returned)
- 10% hang → stays in `processing` (caught by periodic retry)

---

## Railway Deployment

See environment variables required:

```
SECRET_KEY=<generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
DATABASE_URL=<provided by Railway PostgreSQL addon>
REDIS_URL=<provided by Railway Redis addon>
DEBUG=False
ALLOWED_HOSTS=<your-railway-domain>.railway.app
```

Start command: `python manage.py migrate && python manage.py seed_data && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`
