import logging
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import Merchant, BankAccount, PayoutRequest
from .serializers import (
    MerchantSerializer, MerchantDashboardSerializer,
    PayoutRequestSerializer, CreatePayoutSerializer
)
from .services import PayoutService, InsufficientFundsError
from .tasks import process_payout_task

logger = logging.getLogger('payments')


class MerchantListView(APIView):
    def get(self, request):
        merchants = Merchant.objects.all()
        serializer = MerchantSerializer(merchants, many=True)
        return Response(serializer.data)


class MerchantDashboardView(APIView):
    def get(self, request, merchant_id):
        merchant = get_object_or_404(Merchant, id=merchant_id)
        serializer = MerchantDashboardSerializer(merchant)
        return Response(serializer.data)


class PayoutListCreateView(APIView):
    """
    POST /api/v1/payouts
    Headers: Idempotency-Key: <uuid>  (required)
             X-Merchant-ID: <uuid>    (required)
    """

    def get(self, request):
        merchant_id = request.headers.get('X-Merchant-ID') or request.query_params.get('merchant_id')
        if not merchant_id:
            return Response(
                {"error": "X-Merchant-ID header required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        merchant = get_object_or_404(Merchant, id=merchant_id)
        payouts = PayoutRequest.objects.filter(merchant=merchant)
        serializer = PayoutRequestSerializer(payouts, many=True)
        return Response(serializer.data)

    def post(self, request):
        # ── Extract idempotency key ────────────────────────────────────────────
        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key:
            return Response(
                {"error": "Idempotency-Key header is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Identify merchant ──────────────────────────────────────────────────
        merchant_id = request.headers.get('X-Merchant-ID') or request.data.get('merchant_id')
        if not merchant_id:
            return Response(
                {"error": "X-Merchant-ID header required"},
                status=status.HTTP_400_BAD_REQUEST
            )
        merchant = get_object_or_404(Merchant, id=merchant_id)

        # ── Validate input ─────────────────────────────────────────────────────
        serializer = CreatePayoutSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        amount_paise = serializer.validated_data['amount_paise']
        bank_account_id = serializer.validated_data['bank_account_id']

        # ── Idempotency wrapper ────────────────────────────────────────────────
        def do_create():
            """The actual creation logic, called only if key is new."""
            try:
                payout = PayoutService.create_payout(
                    merchant_id=merchant.id,
                    amount_paise=amount_paise,
                    bank_account_id=bank_account_id,
                    idempotency_key_str=idempotency_key,
                )
                response_data = PayoutRequestSerializer(payout).data
                # Queue for processing
                process_payout_task.delay(str(payout.id))
                return status.HTTP_201_CREATED, response_data

            except InsufficientFundsError as e:
                return status.HTTP_422_UNPROCESSABLE_ENTITY, {"error": str(e)}
            except BankAccount.DoesNotExist:
                return status.HTTP_404_NOT_FOUND, {"error": "Bank account not found"}
            except Exception as e:
                logger.exception(f"Unexpected error creating payout: {e}")
                return status.HTTP_500_INTERNAL_SERVER_ERROR, {"error": "Internal server error"}

        resp_status, resp_body, was_cached = PayoutService.get_or_create_idempotency(
            merchant=merchant,
            key_str=idempotency_key,
            create_fn=do_create,
        )

        headers = {}
        if was_cached:
            headers['X-Idempotent-Replayed'] = 'true'

        return Response(resp_body, status=resp_status, headers=headers)


class PayoutDetailView(APIView):
    def get(self, request, payout_id):
        payout = get_object_or_404(PayoutRequest, id=payout_id)
        serializer = PayoutRequestSerializer(payout)
        return Response(serializer.data)


class PayoutStatusView(APIView):
    """Lightweight status-only endpoint for polling."""
    def get(self, request, payout_id):
        payout = get_object_or_404(PayoutRequest, id=payout_id)
        return Response({
            "id": str(payout.id),
            "status": payout.status,
            "updated_at": payout.updated_at,
            "failure_reason": payout.failure_reason,
        })


class BalanceVerifyView(APIView):
    """
    Debug/audit endpoint to verify ledger integrity.
    Confirms: sum(credits) - sum(debits) == displayed balance
    """
    def get(self, request, merchant_id):
        from django.db.models import Sum, Q
        merchant = get_object_or_404(Merchant, id=merchant_id)

        from .models import LedgerEntry
        agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
            credits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.CREDIT)),
            debits=Sum('amount_paise', filter=Q(entry_type=LedgerEntry.DEBIT)),
        )
        credits = agg['credits'] or 0
        debits = agg['debits'] or 0
        derived_balance = credits - debits
        displayed_balance = merchant.get_available_balance()
        invariant_holds = derived_balance == displayed_balance

        return Response({
            "merchant_id": str(merchant.id),
            "total_credits_paise": credits,
            "total_debits_paise": debits,
            "derived_balance_paise": derived_balance,
            "displayed_balance_paise": displayed_balance,
            "invariant_holds": invariant_holds,
        })
