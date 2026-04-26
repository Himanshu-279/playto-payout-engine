import React, { useState, useEffect, useCallback } from 'react';
import { fetchMerchants, fetchDashboard, createPayout, fetchPayoutStatus, verifyBalance } from './api';

const STATUS_COLORS = {
  pending:    'bg-yellow-100 text-yellow-800 border-yellow-200',
  processing: 'bg-blue-100 text-blue-800 border-blue-200',
  completed:  'bg-green-100 text-green-800 border-green-200',
  failed:     'bg-red-100 text-red-800 border-red-200',
};

const Badge = ({ status }) => (
  <span className={`px-2 py-0.5 text-xs font-semibold rounded-full border ${STATUS_COLORS[status] || 'bg-gray-100 text-gray-700'}`}>
    {status}
  </span>
);

const formatINR = (paise) => {
  if (paise === undefined || paise === null) return '—';
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(paise / 100);
};

const formatDate = (dt) => {
  if (!dt) return '—';
  return new Date(dt).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
};

function PayoutRow({ payout, onStatusUpdate }) {
  const [status, setStatus] = useState(payout.status);
  const [checking, setChecking] = useState(false);

  const poll = useCallback(async () => {
    if (status === 'completed' || status === 'failed') return;
    setChecking(true);
    try {
      const res = await fetchPayoutStatus(payout.id);
      setStatus(res.data.status);
      if (onStatusUpdate) onStatusUpdate(payout.id, res.data.status);
    } catch (_) {}
    setChecking(false);
  }, [payout.id, status, onStatusUpdate]);

  useEffect(() => {
    if (status === 'pending' || status === 'processing') {
      const t = setInterval(poll, 3000);
      return () => clearInterval(t);
    }
  }, [status, poll]);

  return (
    <tr className="border-b hover:bg-gray-50 transition-colors">
      <td className="py-3 px-4 font-mono text-xs text-gray-500">{payout.id.slice(0, 8)}…</td>
      <td className="py-3 px-4 font-semibold text-gray-900">{formatINR(payout.amount_paise)}</td>
      <td className="py-3 px-4">
        <div className="flex items-center gap-2">
          <Badge status={status} />
          {checking && <span className="text-xs text-gray-400 animate-pulse">updating…</span>}
        </div>
      </td>
      <td className="py-3 px-4 text-sm text-gray-500">{formatDate(payout.created_at)}</td>
      <td className="py-3 px-4 text-xs text-red-500 max-w-xs truncate">{payout.failure_reason || '—'}</td>
    </tr>
  );
}

function PayoutForm({ merchant, bankAccounts, onSuccess, onError }) {
  const [amountRupees, setAmountRupees] = useState('');
  const [bankAccountId, setBankAccountId] = useState(bankAccounts[0]?.id || '');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState(null);

  // Reset bank account when merchant changes (bankAccounts prop updates)
  useEffect(() => {
    if (bankAccounts.length > 0) {
      const currentStillValid = bankAccounts.some(b => b.id === bankAccountId);
      if (!currentStillValid) {
        setBankAccountId(bankAccounts[0].id);
      }
    }
  }, [bankAccounts, bankAccountId]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setMsg(null);
    const amount = parseFloat(amountRupees);
    if (!amount || amount < 1) {
      setMsg({ type: 'error', text: 'Amount must be at least ₹1' });
      return;
    }
    const amountPaise = Math.round(amount * 100);
    setLoading(true);
    try {
      const res = await createPayout(merchant.id, amountPaise, bankAccountId);
      setMsg({ type: 'success', text: `Payout of ${formatINR(amountPaise)} created! ID: ${res.data.id.slice(0,8)}…` });
      setAmountRupees('');
      if (onSuccess) onSuccess(res.data);
    } catch (err) {
      const errMsg = err.response?.data?.error || 'Failed to create payout';
      setMsg({ type: 'error', text: errMsg });
      if (onError) onError(errMsg);
    }
    setLoading(false);
  };

  return (
    <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-6">
      <h3 className="text-lg font-bold text-gray-900 mb-4">Request Payout</h3>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Amount (₹)</label>
          <div className="relative">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 font-semibold">₹</span>
            <input
              type="number"
              min="1"
              step="0.01"
              value={amountRupees}
              onChange={e => setAmountRupees(e.target.value)}
              placeholder="Enter amount"
              className="w-full pl-8 pr-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition"
              required
            />
          </div>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Bank Account</label>
          <select
            value={bankAccountId}
            onChange={e => setBankAccountId(e.target.value)}
            className="w-full px-3 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 outline-none"
          >
            {bankAccounts.map(b => (
              <option key={b.id} value={b.id}>
                {b.account_holder_name} — {b.account_number_masked} ({b.ifsc_code})
              </option>
            ))}
          </select>
        </div>
        {msg && (
          <div className={`text-sm px-3 py-2 rounded-lg ${msg.type === 'error' ? 'bg-red-50 text-red-700' : 'bg-green-50 text-green-700'}`}>
            {msg.text}
          </div>
        )}
        <button
          type="submit"
          disabled={loading}
          className="w-full bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors"
        >
          {loading ? 'Processing…' : 'Request Payout'}
        </button>
      </form>
    </div>
  );
}

function BalanceCard({ label, amount, color = 'indigo', sub }) {
  const colors = {
    indigo: 'from-indigo-500 to-indigo-600',
    orange: 'from-orange-400 to-orange-500',
    green:  'from-green-500 to-green-600',
  };
  return (
    <div className={`bg-gradient-to-br ${colors[color]} rounded-2xl p-5 text-white shadow-md`}>
      <p className="text-sm font-medium opacity-80">{label}</p>
      <p className="text-3xl font-bold mt-1">{formatINR(amount)}</p>
      {sub && <p className="text-xs opacity-70 mt-1">{sub}</p>}
    </div>
  );
}

export default function App() {
  const [merchants, setMerchants] = useState([]);
  const [selectedMerchantId, setSelectedMerchantId] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [payouts, setPayouts] = useState([]);

  useEffect(() => {
    fetchMerchants()
      .then(res => {
        setMerchants(res.data);
        if (res.data.length > 0) setSelectedMerchantId(res.data[0].id);
      })
      .catch(() => setError('Could not load merchants. Is the backend running?'));
  }, []);

  const loadDashboard = useCallback(async (id) => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchDashboard(id);
      setDashboard(res.data);
      setPayouts(res.data.recent_payouts || []);
    } catch {
      setError('Failed to load dashboard');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    loadDashboard(selectedMerchantId);
  }, [selectedMerchantId, loadDashboard]);

  // Auto-refresh every 5 seconds
  useEffect(() => {
    if (!selectedMerchantId) return;
    const t = setInterval(() => loadDashboard(selectedMerchantId), 5000);
    return () => clearInterval(t);
  }, [selectedMerchantId, loadDashboard]);

  const handlePayoutSuccess = (newPayout) => {
    setPayouts(prev => [newPayout, ...prev]);
    setTimeout(() => loadDashboard(selectedMerchantId), 500);
  };

  const handleStatusUpdate = useCallback((payoutId, newStatus) => {
    setPayouts(prev => prev.map(p => p.id === payoutId ? { ...p, status: newStatus } : p));
  }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10 shadow-sm">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center">
              <span className="text-white font-bold text-sm">P</span>
            </div>
            <div>
              <h1 className="text-lg font-bold text-gray-900">Playto Pay</h1>
              <p className="text-xs text-gray-500">Merchant Dashboard</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <label className="text-sm font-medium text-gray-600">Merchant:</label>
            <select
              value={selectedMerchantId || ''}
              onChange={e => setSelectedMerchantId(e.target.value)}
              className="text-sm border border-gray-300 rounded-lg px-3 py-1.5 focus:ring-2 focus:ring-indigo-500 outline-none"
            >
              {merchants.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded-xl p-4 mb-6 text-sm">
            {error}
          </div>
        )}

        {loading && !dashboard && (
          <div className="flex items-center justify-center py-20">
            <div className="animate-spin w-8 h-8 border-4 border-indigo-500 border-t-transparent rounded-full" />
          </div>
        )}

        {dashboard && (
          <>
            {/* Balance Cards */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
              <BalanceCard
                label="Available Balance"
                amount={dashboard.available_balance_paise}
                color="indigo"
                sub="Ready to withdraw"
              />
              <BalanceCard
                label="Held Balance"
                amount={dashboard.held_balance_paise}
                color="orange"
                sub="In pending/processing payouts"
              />
              <BalanceCard
                label="Total Credits"
                amount={dashboard.available_balance_paise + dashboard.held_balance_paise}
                color="green"
                sub="Lifetime inflow"
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              {/* Payout Form */}
              <div className="lg:col-span-1">
                <PayoutForm
                  merchant={dashboard}
                  bankAccounts={dashboard.bank_accounts}
                  onSuccess={handlePayoutSuccess}
                />

                {/* Ledger Summary */}
                <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-6 mt-4">
                  <h3 className="text-lg font-bold text-gray-900 mb-4">Recent Transactions</h3>
                  <div className="space-y-3 max-h-72 overflow-y-auto">
                    {dashboard.recent_ledger_entries.map(entry => (
                      <div key={entry.id} className="flex items-start justify-between gap-2 pb-3 border-b last:border-0">
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-gray-600 truncate">{entry.description}</p>
                          <p className="text-xs text-gray-400 mt-0.5">{formatDate(entry.created_at)}</p>
                        </div>
                        <span className={`text-sm font-semibold whitespace-nowrap ${entry.entry_type === 'credit' ? 'text-green-600' : 'text-red-600'}`}>
                          {entry.entry_type === 'credit' ? '+' : '-'}{formatINR(entry.amount_paise)}
                        </span>
                      </div>
                    ))}
                    {dashboard.recent_ledger_entries.length === 0 && (
                      <p className="text-sm text-gray-400 text-center py-4">No transactions yet</p>
                    )}
                  </div>
                </div>
              </div>

              {/* Payout History Table */}
              <div className="lg:col-span-2">
                <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
                  <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
                    <h3 className="text-lg font-bold text-gray-900">Payout History</h3>
                    <span className="text-xs text-gray-400 animate-pulse">Live updates every 3s</span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wider">
                        <tr>
                          <th className="py-3 px-4 text-left">ID</th>
                          <th className="py-3 px-4 text-left">Amount</th>
                          <th className="py-3 px-4 text-left">Status</th>
                          <th className="py-3 px-4 text-left">Created</th>
                          <th className="py-3 px-4 text-left">Failure Reason</th>
                        </tr>
                      </thead>
                      <tbody>
                        {payouts.map(payout => (
                          <PayoutRow
                            key={payout.id}
                            payout={payout}
                            onStatusUpdate={handleStatusUpdate}
                          />
                        ))}
                        {payouts.length === 0 && (
                          <tr>
                            <td colSpan={5} className="py-12 text-center text-gray-400 text-sm">
                              No payouts yet. Request your first payout →
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
