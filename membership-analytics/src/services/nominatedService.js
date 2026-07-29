const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';
const FASTAPI_TIMEOUT_MS = Number(process.env.FASTAPI_TIMEOUT_MS || 30000);
const FASTAPI_LIST_TIMEOUT_MS = Number(process.env.FASTAPI_LIST_TIMEOUT_MS || 45000);

async function callFastApi(path, { query = {}, method = 'GET', body = undefined, timeoutMs } = {}) {
  const url = new URL(`${FASTAPI_BASE_URL}${path}`);

  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value);
    }
  });

  const requestId = `bff-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const started = Date.now();

  console.log(`[NOMINATED-BFF] request_start requestId=${requestId} method=${method} url=${url.toString()}`);

  try {
    const fetchOptions = {
      method,
      headers: {
        'X-Request-ID': requestId,
        'Accept': 'application/json',
      },
    };

    if (body !== undefined) {
      fetchOptions.headers['Content-Type'] = 'application/json';
      fetchOptions.body = JSON.stringify(body);
    }

    const isListProposals = method === 'GET' && /\/proposals$/.test(path);
    const timeout = timeoutMs ?? (isListProposals ? FASTAPI_LIST_TIMEOUT_MS : FASTAPI_TIMEOUT_MS);
    fetchOptions.signal = AbortSignal.timeout(timeout);
    const response = await fetch(url, fetchOptions);

    const text = await response.text();
    const elapsed = Date.now() - started;

    console.log(`[NOMINATED-BFF] request_end requestId=${requestId} status=${response.status} elapsedMs=${elapsed}`);

    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      const err = new Error(`FastAPI returned non-JSON response: ${text.slice(0, 300)}`);
      err.statusCode = 502;
      err.requestId = requestId;
      throw err;
    }

    if (!response.ok) {
      // FastAPI validation errors (422): detail is an array of {loc, msg, type}
      // FastAPI app errors: detail is an object with message/error
      // Other errors: message or error string at root
      let message;
      if (Array.isArray(payload?.detail)) {
        message = payload.detail.map(e => `${e.loc?.slice(-1)[0]}: ${e.msg}`).join(', ');
      } else {
        message =
          payload?.detail?.message ||
          payload?.detail?.error ||
          payload?.detail ||
          payload?.message ||
          payload?.error ||
          `FastAPI error ${response.status}`;
      }

      const err = new Error(message);
      err.statusCode = response.status;
      err.payload = payload;
      err.requestId = requestId;
      throw err;
    }

    return payload;
  } catch (error) {
    const elapsed = Date.now() - started;
    const cause = error.cause?.message || error.cause || '';
    console.error(`[NOMINATED-BFF] request_failed requestId=${requestId} elapsedMs=${elapsed} error=${error.message}${cause ? ` cause=${cause}` : ''}`);

    if (!error.statusCode) {
      error.statusCode = 502;
      error.message = `Unable to connect to FastAPI service at ${FASTAPI_BASE_URL}. ${error.message}${cause ? `: ${cause}` : ''}`;
    }

    throw error;
  }
}

module.exports = {
  nominatedBootstrap:    (q) => callFastApi('/api/v1/nominated-post/bootstrap',   { query: q }),
  nominatedLocations:    (q) => callFastApi('/api/v1/nominated-post/locations',    { query: q }),
  nominatedDepartments:  (q) => callFastApi('/api/v1/nominated-post/departments',  { query: q }),
  nominatedBoards:       (q) => callFastApi('/api/v1/nominated-post/boards',       { query: q }),
  nominatedPositions:    (q) => callFastApi('/api/v1/nominated-post/positions',    { query: q }),
  nominatedCapacity:     (q) => callFastApi('/api/v1/nominated-post/capacity',     { query: q }),
  searchCadre:           (q) => callFastApi('/api/v1/nominated-post/cadre/search', { query: q }),
  getCasteOptions:       (q) => callFastApi('/api/v1/nominated-post/caste-options', { query: q }),
  updateCandidateCaste:  (body) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/caste', { method: 'PATCH', body }),
  getOccupationOptions:  (q) => callFastApi('/api/v1/nominated-post/occupation-options', { query: q }),
  updateCandidateOccupation: (body) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/occupation', { method: 'PATCH', body }),
  getEducationOptions:   (q) => callFastApi('/api/v1/nominated-post/education-options', { query: q }),
  getPartyOptions:       () => callFastApi('/api/v1/nominated-post/party-options'),
  updateCandidateEducation: (body) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/education', { method: 'PATCH', body }),

  listProposals:         (q) => callFastApi('/api/v1/nominated-post/proposals', { query: q }),
  createProposal:        (body) => callFastApi('/api/v1/nominated-post/proposals', { method: 'POST', body }),
  getProposal:           (proposalId) => callFastApi(`/api/v1/nominated-post/proposals/${proposalId}`),
  updateProposalStatus:  (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/status`, { method: 'PATCH', body }),
  addProposalCandidates: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/candidates`, { method: 'POST', body }),
  createManualCandidate: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/candidates/manual`, { method: 'POST', body }),
  removeProposalCandidate: (proposalId, proposalCandidateId, q) =>
    callFastApi(
      `/api/v1/nominated-post/proposals/${proposalId}/candidates/${proposalCandidateId}`,
      { method: 'DELETE', query: q },
    ),
  removeAllProposalCandidates: (proposalId, q) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/candidates`, {
      method: 'DELETE',
      query: q,
    }),
  deleteProposal: (proposalId, q = {}) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/delete`, {
      method: 'DELETE',
      query: q,
    }),
  revertProposalStage: (proposalId, q = {}) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/revert-stage`, {
      method: 'POST',
      query: q,
    }),
  searchProposalCandidates: (q) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/search', { query: q }),

  // Candidate Pool — cadres from report_ratings.cadre_details ranked by performance score.
  listCandidatePool: (q) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/pool', {
      query: q,
      timeoutMs: 60000,
    }),

  // Candidate Pool compare — reads only the _nom snapshot + feedback tables (no procedures).
  comparePoolCandidates: (body) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/pool/compare', {
      method: 'POST',
      body,
      timeoutMs: 60000,
    }),

  // MY TDP APP USAGE — the constituency_rank query is heavy (~tens of seconds),
  // so allow a generous timeout.
  getCandidateAppUsage: (q) =>
    callFastApi('/api/v1/nominated-post/proposals/candidates/app-usage', {
      query: q,
      timeoutMs: 60000,
    }),

  compareProposalCandidates: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/proposals/${proposalId}/candidates/compare`, {
      method: 'POST',
      body,
      timeoutMs: 60000,
    }),

  // Workflow APIs
  getWorkflowActions: (proposalId, q) =>
    callFastApi(`/api/v1/nominated-post/workflow/proposals/${proposalId}/actions`, {
      method: 'GET',
      query: q,
    }),

  executeWorkflowAction: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/workflow/proposals/${proposalId}/actions`, {
      method: 'POST',
      body,
    }),

  saveProposalFeedbacks: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/workflow/proposals/${proposalId}/feedbacks`, {
      method: 'POST',
      body,
    }),

  saveWorkflowReviews: (proposalId, body) =>
    callFastApi(`/api/v1/nominated-post/workflow/proposals/${proposalId}/reviews`, {
      method: 'PUT',
      body,
    }),

  getWorkflowHistory: (proposalId) =>
    callFastApi(`/api/v1/nominated-post/workflow/proposals/${proposalId}/history`, {
      method: 'GET',
    }),
};
