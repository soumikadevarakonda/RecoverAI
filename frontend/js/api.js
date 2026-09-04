/**
 * RecoverAI API Client Layer
 * Centralized HTTP communication for Merchant APIs.
 */

const RecoverAPI = (function () {
    const CURRENT_TRIAL_MERCHANT_ID = 'b614b90f-49fd-4d6a-8689-52d4d2878b03';

    // Determine API Base URL: supports direct backend or local proxy
    const API_BASE = window.location.port === '8000' 
        ? '/api/v1' 
        : (window.location.origin.includes('5174') || window.location.origin.includes('5173') ? '/api/v1' : 'http://127.0.0.1:8000/api/v1');

    // Resolve merchant ID: use current trial merchant ID, ignoring obsolete legacy IDs in localStorage
    let storedMerchantId = localStorage.getItem('recoverai_merchant_id');
    if (!storedMerchantId || storedMerchantId === '45b88de2-a84e-49b0-ad8d-880a9ab12ab0') {
        storedMerchantId = CURRENT_TRIAL_MERCHANT_ID;
        localStorage.setItem('recoverai_merchant_id', CURRENT_TRIAL_MERCHANT_ID);
    }
    let merchantId = storedMerchantId;

    function getHeaders() {
        return {
            'Content-Type': 'application/json',
            'X-Merchant-ID': merchantId
        };
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
                const error = new Error(errorData.detail || `Request failed with status ${response.status}`);
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

    return {
        getMerchantId() {
            return merchantId;
        },
        setMerchantId(id) {
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
            return request(`/merchant/incidents/${incidentId}`);
        },

        // Recovery Attempts
        async getRecoveries() {
            return request('/merchant/recoveries');
        },
        async getRecoveryDetail(recoveryId) {
            return request(`/merchant/recoveries/${recoveryId}`);
        },

        // Recovery Actions
        async approveRecovery(recoveryId) {
            return request(`/merchant/recoveries/${recoveryId}/approve`, {
                method: 'POST'
            });
        },
        async executeRecovery(recoveryId) {
            return request(`/merchant/recoveries/${recoveryId}/execute`, {
                method: 'POST'
            });
        },

        // Audit Trail
        async getRecoveryAudit(recoveryId) {
            return request(`/merchant/recoveries/${recoveryId}/audit`);
        },
        async getCampaignAudit(campaignId) {
            return request(`/merchant/campaigns/${campaignId}/audit`);
        }
    };
})();

// Export globally
window.RecoverAPI = RecoverAPI;
