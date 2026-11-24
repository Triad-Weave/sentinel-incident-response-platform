import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { IncidentDetailResponse, User } from '../types';

interface FindingCardProps {
  detail: IncidentDetailResponse;
  user: User;
  onOpenEvidence: (evidenceId: string) => void;
}

export function FindingCard({ detail, user, onOpenEvidence }: FindingCardProps) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const approveMutation = useMutation({
    mutationFn: () => api.approveAction(detail.action_request!.id, reason.trim()),
    onSuccess: () => {
      setReason('');
      setErrorMsg(null);
      queryClient.invalidateQueries({ queryKey: ['incident', detail.incident.id] });
    },
    onError: (err: Error) => setErrorMsg(err.message),
  });

  const rejectMutation = useMutation({
    mutationFn: () => api.rejectAction(detail.action_request!.id, reason.trim()),
    onSuccess: () => {
      setReason('');
      setErrorMsg(null);
      queryClient.invalidateQueries({ queryKey: ['incident', detail.incident.id] });
    },
    onError: (err: Error) => setErrorMsg(err.message),
  });

  const retryMutation = useMutation({
    mutationFn: () => api.retryIncident(detail.incident.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['incident', detail.incident.id] });
    },
    onError: (err: Error) => setErrorMsg(err.message),
  });

  const { incident, investigation, finding, action_request } = detail;

  if (incident.status === 'Investigation failed') {
    return (
      <div className="card">
        <h3 style={{ marginTop: 0, color: '#991b1b' }}>Investigation failed</h3>
        <p>{investigation?.failure_reason ?? 'Model or worker error interrupted investigation.'}</p>
        {user.role === 'responder' ? (
          <button
            className="btn btn-primary"
            onClick={() => retryMutation.mutate()}
            disabled={retryMutation.isPending}
          >
            {retryMutation.isPending ? 'Retrying…' : 'Retry investigation'}
          </button>
        ) : (
          <p className="incident-meta">Viewers: read-only, no retry button</p>
        )}
        {errorMsg && <p className="error-text">{errorMsg}</p>}
      </div>
    );
  }

  if (!finding) {
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Finding</h3>
        <p className="incident-meta">Investigation in progress — gathering evidence via MCP tools…</p>
      </div>
    );
  }

  const canDecide =
    user.role === 'responder' && action_request && action_request.status === 'proposed';
  const reasonValid = reason.trim().length >= 3 && reason.trim().length <= 500;

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: 0 }}>Finding</h3>
        <span
          className={`pill ${
            finding.confidence === 'high'
              ? 'pill-blue'
              : finding.confidence === 'medium'
                ? 'pill-amber'
                : 'pill-gray'
          }`}
        >
          {finding.confidence}
        </span>
      </div>

      <p style={{ fontWeight: 700, marginBottom: 6 }}>
        {finding.suspected_service} · {finding.fault_type}
      </p>
      <p style={{ marginTop: 0, fontSize: 14 }}>{finding.body.hypothesis}</p>

      <div className="chips-row">
        {finding.body.evidence.map((ev) => (
          <button
            key={ev.evidence_id}
            type="button"
            className="ev-chip"
            title={ev.claim}
            onClick={() => onOpenEvidence(ev.evidence_id)}
          >
            {ev.evidence_id}
          </button>
        ))}
      </div>

      {finding.guardrail_notes.length > 0 && (
        <div className="banner" style={{ flexDirection: 'column', alignItems: 'flex-start' }}>
          <strong>Guardrails applied:</strong>
          {finding.guardrail_notes.map((n, idx) => (
            <span key={idx}>• {n}</span>
          ))}
        </div>
      )}

      {action_request && (
        <div style={{ borderTop: '1px solid #dfe3ea', paddingTop: 14, marginTop: 14 }}>
          <div className="incident-meta">Proposed action</div>
          <div style={{ fontWeight: 700, margin: '4px 0 8px' }}>
            {action_request.action === 'rollback'
              ? `Roll back ${action_request.service}${action_request.from_version ? ` (${action_request.from_version})` : ''}`
              : action_request.action === 'restart'
                ? `Rolling restart ${action_request.service}`
                : 'No automated action (monitor)'}
          </div>
          <p className="incident-meta" style={{ marginTop: 0 }}>
            {finding.body.proposed_action.rationale}
          </p>

          {action_request.status !== 'proposed' ? (
            <div style={{ fontSize: 13, marginTop: 10 }}>
              <strong>Status:</strong> {action_request.status}
              {action_request.decided_by && (
                <div>
                  By {action_request.decided_by}: “{action_request.decision_reason}”
                </div>
              )}
            </div>
          ) : user.role === 'responder' ? (
            <div style={{ marginTop: 10 }}>
              <input
                type="text"
                style={{ width: '100%' }}
                placeholder="Reason (required, 3–500 chars)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <div className="btn-row">
                <button
                  className="btn btn-primary"
                  disabled={!canDecide || !reasonValid || approveMutation.isPending}
                  onClick={() => approveMutation.mutate()}
                >
                  Approve
                </button>
                <button
                  className="btn btn-danger"
                  disabled={!canDecide || !reasonValid || rejectMutation.isPending}
                  onClick={() => rejectMutation.mutate()}
                >
                  Reject
                </button>
              </div>
              {errorMsg && <p className="error-text">{errorMsg}</p>}
            </div>
          ) : (
            <p className="incident-meta" style={{ marginTop: 12 }}>
              Viewers: read-only, no buttons
            </p>
          )}
        </div>
      )}
    </div>
  );
}
