// Add these functions to src/services/committeeService.js
// If you prefer a single nominatedService.js style file, you can merge these exports there.

const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://localhost:8001';

async function callFastApi(path, { query = {}, method = 'GET', body = undefined } = {}) {
  const url = new URL(`${FASTAPI_BASE_URL}${path}`);

  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value);
    }
  });

  const response = await fetch(url.toString(), {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(data?.detail?.message || data?.message || 'FastAPI request failed');
    // errorMiddleware reads `statusCode` (not `status`); using the wrong property
    // collapsed every FastAPI error — 400/409 included — into a generic 500.
    err.statusCode = response.status;
    err.payload = data;
    throw err;
  }
  return data;
}

module.exports = {
  getBasicCommittees: (q) =>
    callFastApi('/api/v1/committee/basic-committees', { method: 'GET', query: q }),

  getCommittees: (q) =>
    callFastApi('/api/v1/committee/committees', { method: 'GET', query: q }),

  getCommitteeRoles: (q) =>
    callFastApi('/api/v1/committee/roles', { method: 'GET', query: q }),

  checkKss: (q) =>
    callFastApi('/api/v1/committee/kss-check', { method: 'GET', query: q }),

  listCommitteeProposals: (q) =>
    callFastApi('/api/v1/committee/proposals', { method: 'GET', query: q }),

  createCommitteeProposal: (body) =>
    callFastApi('/api/v1/committee/proposals', { method: 'POST', body }),

  addCommitteeMembers: (proposalId, body) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/members`, { method: 'POST', body }),

  compareCommitteeCandidates: (proposalId, body) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/candidates/compare`, { method: 'POST', body }),

  removeCommitteeMember: (proposalId, memberId, query) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/members/${memberId}`, { method: 'DELETE', query }),

  removeAllCommitteeMembers: (proposalId, query) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/members`, { method: 'DELETE', query }),

  deleteCommitteeProposal: (proposalId, query = {}) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/delete`, { method: 'DELETE', query }),

  revertCommitteeStage: (proposalId, query = {}) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/revert-stage`, { method: 'POST', query }),

  updateCommitteeStatus: (proposalId, body) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}/status`, { method: 'PATCH', body }),

  getCommitteeProposal: (proposalId) =>
    callFastApi(`/api/v1/committee/proposals/${proposalId}`, { method: 'GET' }),

  getCommitteeWorkflowActions: (proposalId, q) =>
    callFastApi(`/api/v1/committee/workflow/proposals/${proposalId}/actions`, { method: 'GET', query: q }),

  executeCommitteeWorkflowAction: (proposalId, body) =>
    callFastApi(`/api/v1/committee/workflow/proposals/${proposalId}/actions`, { method: 'POST', body }),

  saveCommitteeWorkflowFeedbacks: (proposalId, body) =>
    callFastApi(`/api/v1/committee/workflow/proposals/${proposalId}/feedbacks`, { method: 'POST', body }),

  saveCommitteeWorkflowReviews: (proposalId, body) =>
    callFastApi(`/api/v1/committee/workflow/proposals/${proposalId}/reviews`, { method: 'PUT', body }),

  getCommitteeWorkflowHistory: (proposalId) =>
    callFastApi(`/api/v1/committee/workflow/proposals/${proposalId}/history`, { method: 'GET' }),
};
