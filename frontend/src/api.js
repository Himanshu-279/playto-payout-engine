
const BASE_URL = 'https://playto-payout-engine-production-5fd9.up.railway.app';

const apiGet = (path) =>
  fetch(BASE_URL + '/api/v1' + path).then(r => r.json());

const apiPost = (path, body, headers) =>
  fetch(BASE_URL + '/api/v1' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  }).then(r => r.json().then(data => ({ data, status: r.status })));

export const fetchMerchants = () => apiGet('/merchants/').then(data => ({ data }));
export const fetchDashboard = (merchantId) => apiGet('/merchants/' + merchantId + '/').then(data => ({ data }));
export const fetchPayoutStatus = (payoutId) => apiGet('/payouts/' + payoutId + '/status/').then(data => ({ data }));
export const fetchPayouts = (merchantId) => apiGet('/payouts/?merchant_id=' + merchantId).then(data => ({ data }));

export const createPayout = (merchantId, amountPaise, bankAccountId) => {
  const idempotencyKey = crypto.randomUUID();
  return apiPost(
    '/payouts/',
    { amount_paise: amountPaise, bank_account_id: bankAccountId },
    { 'Idempotency-Key': idempotencyKey, 'X-Merchant-ID': merchantId }
  ).then(({ data, status }) => {
    if (status >= 400) throw { response: { data } };
    return { data };
  });
};

export const verifyBalance = (merchantId) => apiGet('/merchants/' + merchantId + '/balance/verify/').then(data => ({ data }));
