from django.urls import path
from . import views

urlpatterns = [
    path('merchants/', views.MerchantListView.as_view(), name='merchant-list'),
    path('merchants/<uuid:merchant_id>/', views.MerchantDashboardView.as_view(), name='merchant-dashboard'),
    path('merchants/<uuid:merchant_id>/balance/verify/', views.BalanceVerifyView.as_view(), name='balance-verify'),
    path('payouts/', views.PayoutListCreateView.as_view(), name='payout-list-create'),
    path('payouts/<uuid:payout_id>/', views.PayoutDetailView.as_view(), name='payout-detail'),
    path('payouts/<uuid:payout_id>/status/', views.PayoutStatusView.as_view(), name='payout-status'),
]
