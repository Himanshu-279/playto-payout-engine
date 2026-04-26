from django.contrib import admin
from .models import Merchant, LedgerEntry, BankAccount, PayoutRequest, IdempotencyKey

@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'created_at']
    search_fields = ['name', 'email']

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ['merchant', 'entry_type', 'amount_paise', 'description', 'created_at']
    list_filter = ['entry_type', 'merchant']
    search_fields = ['description']

@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ['account_holder_name', 'merchant', 'ifsc_code', 'is_primary']

@admin.register(PayoutRequest)
class PayoutRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'merchant', 'amount_paise', 'status', 'retry_count', 'created_at']
    list_filter = ['status']
    search_fields = ['merchant__name']

@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ['key', 'merchant', 'response_status_code', 'created_at', 'expires_at']
