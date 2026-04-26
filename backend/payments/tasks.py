"""
Celery tasks for payout processing.
All tasks use proper DB transactions and state machine validation.
"""
import random
import logging
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db import transaction

from .models import PayoutRequest
from .services import PayoutService

logger = logging.getLogger('payments')

PROCESSING_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
BACKOFF_BASE = 5  # seconds


@shared_task(bind=True, max_retries=MAX_RETRIES)
def process_payout_task(self, payout_id):
    """
    Main payout processor. Simulates bank settlement:
    - 70% success
    - 20% failure
    - 10% hung in processing (will be caught by retry_stuck_payouts)
    """
    logger.info(f"Processing payout {payout_id}, attempt {self.request.retries + 1}")

    try:
        payout = PayoutService.process_payout(payout_id)
        if payout is None:
            logger.error(f"Payout {payout_id} not found")
            return

        if payout.status != PayoutRequest.PROCESSING:
            logger.warning(f"Payout {payout_id} not in processing state, skipping")
            return

        # Simulate bank API call
        outcome = random.random()

        if outcome < 0.70:
            # 70% success
            PayoutService.complete_payout(payout_id)
            logger.info(f"Payout {payout_id} completed successfully")

        elif outcome < 0.90:
            # 20% failure
            PayoutService.fail_payout(
                payout_id,
                reason="Bank rejected: insufficient beneficiary details"
            )
            logger.info(f"Payout {payout_id} failed at bank")

        else:
            # 10% hang — stay in processing, retry_stuck_payouts will handle it
            logger.warning(f"Payout {payout_id} is hanging in processing state")

    except Exception as exc:
        logger.exception(f"Error processing payout {payout_id}: {exc}")
        # Exponential backoff retry
        backoff = BACKOFF_BASE * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=backoff)


@shared_task
def retry_stuck_payouts():
    """
    Periodic task: runs every 30 seconds.
    Finds payouts stuck in PROCESSING for > 30 seconds and retries or fails them.
    """
    cutoff = timezone.now() - timedelta(seconds=PROCESSING_TIMEOUT_SECONDS)

    stuck_payouts = PayoutRequest.objects.filter(
        status=PayoutRequest.PROCESSING,
        processing_started_at__lt=cutoff
    ).select_related('merchant')

    count = stuck_payouts.count()
    if count > 0:
        logger.info(f"Found {count} stuck payouts to retry")

    for payout in stuck_payouts:
        if payout.retry_count < payout.max_retries:
            # Increment retry count and re-queue
            with transaction.atomic():
                PayoutRequest.objects.filter(id=payout.id).update(
                    retry_count=payout.retry_count + 1,
                    status=PayoutRequest.PENDING,
                    processing_started_at=None,
                )
            backoff = BACKOFF_BASE * (2 ** payout.retry_count)
            process_payout_task.apply_async(
                args=[str(payout.id)],
                countdown=backoff
            )
            logger.info(
                f"Requeued stuck payout {payout.id}, "
                f"attempt {payout.retry_count + 1}/{payout.max_retries}"
            )
        else:
            # Max retries exceeded — fail and return funds
            try:
                PayoutService.fail_payout(
                    str(payout.id),
                    reason=f"Max retries ({payout.max_retries}) exceeded. Bank unresponsive."
                )
                logger.warning(
                    f"Payout {payout.id} permanently failed after {payout.max_retries} retries"
                )
            except Exception as e:
                logger.exception(f"Error failing stuck payout {payout.id}: {e}")


@shared_task
def trigger_pending_payouts():
    """
    Pick up all pending payouts and queue them for processing.
    Runs on startup and can be called manually.
    """
    pending = PayoutRequest.objects.filter(status=PayoutRequest.PENDING)
    for payout in pending:
        process_payout_task.delay(str(payout.id))
        logger.info(f"Queued pending payout {payout.id}")
