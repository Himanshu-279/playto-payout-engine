from rest_framework import serializers
from .models import Merchant, LedgerEntry, BankAccount, PayoutRequest
import uuid


class BankAccountSerializer(serializers.ModelSerializer):
    account_number_masked = serializers.SerializerMethodField()

    class Meta:
        model = BankAccount
        fields = ['id', 'account_number_masked', 'ifsc_code', 'account_holder_name', 'is_primary']

    def get_account_number_masked(self, obj):
        n = obj.account_number
        return '*' * (len(n) - 4) + n[-4:]


class LedgerEntrySerializer(serializers.ModelSerializer):
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = ['id', 'entry_type', 'amount_paise', 'amount_rupees',
                  'description', 'reference_type', 'created_at']

    def get_amount_rupees(self, obj):
        return obj.amount_paise / 100


class PayoutRequestSerializer(serializers.ModelSerializer):
    amount_rupees = serializers.SerializerMethodField()
    bank_account = BankAccountSerializer(read_only=True)

    class Meta:
        model = PayoutRequest
        fields = ['id', 'amount_paise', 'amount_rupees', 'status', 'bank_account',
                  'failure_reason', 'retry_count', 'created_at', 'updated_at',
                  'processing_started_at']

    def get_amount_rupees(self, obj):
        return obj.amount_paise / 100


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ['id', 'name', 'email']


class MerchantDashboardSerializer(serializers.ModelSerializer):
    available_balance_paise = serializers.SerializerMethodField()
    held_balance_paise = serializers.SerializerMethodField()
    available_balance_rupees = serializers.SerializerMethodField()
    held_balance_rupees = serializers.SerializerMethodField()
    bank_accounts = BankAccountSerializer(many=True, read_only=True)
    recent_ledger_entries = serializers.SerializerMethodField()
    recent_payouts = serializers.SerializerMethodField()

    class Meta:
        model = Merchant
        fields = [
            'id', 'name', 'email',
            'available_balance_paise', 'held_balance_paise',
            'available_balance_rupees', 'held_balance_rupees',
            'bank_accounts', 'recent_ledger_entries', 'recent_payouts',
        ]

    def get_available_balance_paise(self, obj):
        return obj.get_available_balance()

    def get_held_balance_paise(self, obj):
        return obj.get_held_balance()

    def get_available_balance_rupees(self, obj):
        return obj.get_available_balance() / 100

    def get_held_balance_rupees(self, obj):
        return obj.get_held_balance() / 100

    def get_recent_ledger_entries(self, obj):
        entries = obj.ledger_entries.all()[:20]
        return LedgerEntrySerializer(entries, many=True).data

    def get_recent_payouts(self, obj):
        payouts = obj.payout_requests.all()[:20]
        return PayoutRequestSerializer(payouts, many=True).data


class CreatePayoutSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=100)  # minimum 1 rupee
    bank_account_id = serializers.UUIDField()

    def validate_amount_paise(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be positive")
        return value

    def validate_bank_account_id(self, value):
        try:
            uuid.UUID(str(value))
        except ValueError:
            raise serializers.ValidationError("Invalid bank account ID")
        return value
