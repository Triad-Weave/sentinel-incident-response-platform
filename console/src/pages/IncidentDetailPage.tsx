import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, useIncidentStream } from '../api/client';
import { FindingCard } from '../components/FindingCard';
import { FollowupPanel } from '../components/FollowupPanel';
import { StatusBadge } from '../components/StatusBadge';
import { StepDrawer } from '../components/StepDrawer';
import type { User } from '../types';

export function IncidentDetailPage({ user }: { user: User }) {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const [selectedStepSeq, setSelectedStepSeq] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ['incident', id],
    queryFn: () => api.getIncident(id!),
    enabled: Boolean(id),
    refetchInterval: 15_000,
  });

  const { events, connected } = useIncidentStream(id!);

  const resolveMutation = useMutation({
    mutationFn: () => api.resolveIncident(id!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['incident', id] }),
  });

  const retryMutation = useMutation({
    mutationFn: () => api.retryIncident(id!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['incident', id] }),
  });

  if (isLoading) return <div className="page">Loading incident…</div>;
  if (error || !data) return <div className="page error-text">{(error as Error)?.message ?? 'Not found'}</div>;

  const { incident, investigation, alerts } = data;
  const primaryAlert = alerts[0];
  const openEvidenceById = (evidenceId: string) => {
    const match = investigation?.steps.find((s) => s.evidence_id === evidenceId);
    if (match) setSelectedStepSeq(match.seq);
  };

  return (
    <div className="page">
      <div style={{ marginBottom: 12 }}>
        <Link to="/" className="nav-link">
          ← All incidents
        </Link>
      </div>

      <div className="incident-header">
        <div>
          <h1 className="incident-title">
            {incident.short_id} · {incident.title}
          </h1>
          <div className="incident-meta">
            Started {new Date(incident.started_at).toLocaleTimeString()}
            {primaryAlert ? ` · ${primaryAlert.alertname}` : ''} · {incident.alert_service}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <StatusBadge status={incident.status} />
          {data.postmortem_available && (
            <a
              className="btn"
              href={`/api/v1/incidents/${incident.id}/postmortem.md`}
              download={`${incident.short_id.toLowerCase()}-postmortem.md`}
            >
              Download Markdown
            </a>
          )}
          {user.role === 'responder' && incident.status !== 'Resolved' && (
            <button
              className="btn"
              onClick={() => resolveMutation.mutate()}
              disabled={resolveMutation.isPending}
            >
              Resolve
            </button>
          )}
        </div>
      </div>

      {incident.new_evidence_banner && (
        <div className="banner">
          <span>New evidence: another service alerted after the current finding was posted.</span>
          {user.role === 'responder' && (
            <button className="btn btn-primary" onClick={() => retryMutation.mutate()}>
              Retry
            </button>
          )}
        </div>
      )}

      <div className="two-col">
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ margin: 0 }}>Live timeline</h3>
            <span className="live-indicator">
              <span className={`dot ${connected ? 'dot-green' : 'dot-gray'}`} />
              streaming
            </span>
          </div>

          <ul className="timeline-list">
            {events.map((ev) => {
              const p = ev.payload ?? {};
              const stepSeq = typeof p.seq === 'number' ? p.seq : null;
              const evidenceId = typeof p.evidence_id === 'string' ? p.evidence_id : null;
              const toolName = typeof p.tool_name === 'string' ? p.tool_name : null;
              const summary = typeof p.summary === 'string' ? p.summary : null;
              const reasoning = typeof p.reasoning === 'string' ? p.reasoning : null;

              return (
                <li
                  key={ev.seq}
                  className={`timeline-item ${stepSeq ? 'clickable' : ''}`}
                  onClick={() => {
                    if (stepSeq !== null) setSelectedStepSeq(stepSeq);
                  }}
                >
                  <span className="timeline-time">
                    {new Date(ev.created_at).toLocaleTimeString()}
                  </span>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>
                      {toolName ?? ev.type}
                    </div>
                    {reasoning && <div className="timeline-why">why: {reasoning}</div>}
                    {summary && <div className="timeline-summary">{summary}</div>}
                    {!summary && ev.type === 'finding.posted' && (
                      <div className="timeline-summary">
                        {String(p.fault_type)} in {String(p.suspected_service)}, confidence{' '}
                        {String(p.confidence)}
                      </div>
                    )}
                    {!summary && (ev.type === 'action.approved' || ev.type === 'action.rejected') && (
                      <div className="timeline-summary">
                        {ev.actor}: {String(p.reason ?? '')}
                      </div>
                    )}
                  </div>
                  <div>
                    {evidenceId && <span className="ev-chip">{evidenceId}</span>}
                  </div>
                </li>
              );
            })}
          </ul>

          {investigation && (
            <div className="timeline-footer">
              {investigation.tool_calls} tool calls ·{' '}
              {(investigation.input_tokens + investigation.output_tokens).toLocaleString()} tokens ·
              USD {investigation.cost_usd.toFixed(4)}
            </div>
          )}
        </div>

        <div>
          <FindingCard detail={data} user={user} onOpenEvidence={openEvidenceById} />
          <FollowupPanel detail={data} user={user} />
        </div>
      </div>

      {selectedStepSeq !== null && investigation && (
        <StepDrawer
          investigationId={investigation.id}
          seq={selectedStepSeq}
          onClose={() => setSelectedStepSeq(null)}
        />
      )}
    </div>
  );
}
