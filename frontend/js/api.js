/**
 * RecoverAI API Client Layer
 * Centralized HTTP communication for Merchant APIs.
 *
 * Configuration (all optional):
 *   - API base:  window.RECOVERAI_API_BASE, or localStorage 'recoverai_api_base'.
 *                Defaults to '/api/v1' when served by frontend/serve.py (which
 *                proxies to the backend), else http://127.0.0.1:8000/api/v1
 *                (the backend allows the dev origins via CORS).
 *   - Merchant:  ?merchant=<uuid> in the page URL (persisted), or
 *                localStorage 'recoverai_merchant_id'. Defaults to the trial merchant.
 *   - Token:     localStorage 'recoverai_api_token' -> sent as "Authorization: Bearer".
 *                Required when the backend runs with AUTH_MODE=production
 *                (mint one with backend/scripts/issue_merchant_token.py).
 */

const RecoverAPI = (function () {
    const TRIAL_MERCHANT_ID = 'b614b90f-49fd-4d6a-8689-52d4d2878b03';
    const LEGACY_MERCHANT_IDS = ['45b88de2-a84e-49b0-ad8d-880a9ab12ab0'];
    const PROXY_PORTS = ['5173', '5174'];
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    function resolveApiBase() {
        const configured = window.RECOVERAI_API_BASE || localStorage.getItem('recoverai_api_base');
        if (configured) return configured.replace(/\/$/, '');
        if (window.location.protocol.startsWith('http') && PROXY_PORTS.includes(window.location.port)) {
            return '/api/v1';
        }
        return 'http://127.0.0.1:8000/api/v1';
    }

    function resolveMerchantId() {
        const fromQuery = new URLSearchParams(window.location.search).get('merchant');
        if (fromQuery && UUID_RE.test(fromQuery)) {
            localStorage.setItem('recoverai_merchant_id', fromQuery);
            return fromQuery;
        }
        const stored = localStorage.getItem('recoverai_merchant_id');
        if (stored && UUID_RE.test(stored) && !LEGACY_MERCHANT_IDS.includes(stored)) {
            return stored;
        }
        localStorage.setItem('recoverai_merchant_id', TRIAL_MERCHANT_ID);
        return TRIAL_MERCHANT_ID;
    }

    const API_BASE = resolveApiBase();
    let merchantId = resolveMerchantId();

    function getHeaders() {
        const headers = {
            'Content-Type': 'application/json',
            'X-Merchant-ID': merchantId
        };
        const token = localStorage.getItem('recoverai_api_token');
        if (token) headers['Authorization'] = `Bearer ${token}`;
        return headers;
    }

    async function request(endpoint, options = {}) {
        const url = `${API_BASE}${endpoint}`;
        const headers = { ...getHeaders(), ...(options.headers || {}) };

        try {
            const response = await fetch(url, {
                ...options,
                headers
            });

            if (!response.ok) {
                let errorData;
                try {
                    errorData = await response.json();
                } catch (e) {
                    errorData = { detail: response.statusText };
                }
                const detail = errorData.detail;
                const message = typeof detail === 'string'
                    ? detail
                    : (detail && (detail.explanation || detail.message)) || `Request failed with status ${response.status}`;
                const error = new Error(message);
                error.status = response.status;
                error.data = errorData;
                throw error;
            }

            return await response.json();
        } catch (err) {
            console.error(`API Error [${options.method || 'GET'} ${endpoint}]:`, err);
            throw err;
        }
    }

    const enc = encodeURIComponent;

    return {
        getMerchantId() {
            return merchantId;
        },
        setMerchantId(id) {
            if (!UUID_RE.test(id)) throw new Error('Merchant id must be a UUID');
            merchantId = id;
            localStorage.setItem('recoverai_merchant_id', id);
        },
        getApiBase() {
            return API_BASE;
        },

        // Merchant Dashboard Summary
        async getDashboardSummary() {
            return request('/merchant/dashboard/summary');
        },

        // Incidents
        async getIncidents() {
            return request('/merchant/incidents');
        },
        async getIncidentDetail(incidentId) {
            return request(`/merchant/incidents/${enc(incidentId)}`);
        },

        // Recovery Attempts
        async getRecoveries() {
            return request('/merchant/recoveries');
        },
        async getRecoveryDetail(recoveryId) {
            return request(`/merchant/recoveries/${enc(recoveryId)}`);
        },

        // Recovery Actions
        async approveRecovery(recoveryId) {
            return request(`/merchant/recoveries/${enc(recoveryId)}/approve`, {
                method: 'POST'
            });
        },
        async executeRecovery(recoveryId) {
            return request(`/merchant/recoveries/${enc(recoveryId)}/execute`, {
                method: 'POST'
            });
        },

        // Detection cycle (detect -> diagnose -> decide -> campaign)
        async runDetection() {
            return request('/merchant/detection/run', { method: 'POST' });
        },

        // Development checkout simulator: settles through the backend's
        // webhook verification path. Unavailable in production.
        async simulatePaymentLinkPaid(recoveryId) {
            return request(`/dev/payment-links/${enc(recoveryId)}/simulate-paid`, {
                method: 'POST'
            });
        },

        // Audit Trail
        async getRecoveryAudit(recoveryId) {
            return request(`/merchant/recoveries/${enc(recoveryId)}/audit`);
        },
        async getCampaignAudit(campaignId) {
            return request(`/merchant/campaigns/${enc(campaignId)}/audit`);
        }
    };
})();

// Export globally
window.RecoverAPI = RecoverAPI;
