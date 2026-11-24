import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { FindingCard } from '../src/components/FindingCard';
import type { IncidentDetailResponse } from '../src/types';

const sampleDetail: IncidentDetailResponse = {
  incident: {
    id: 'inc-uuid-1',
    short_id: 'INC-0042',
    status: 'Awaiting decision',
    title: 'Orders error rate high',
    alert_service: 'orders',
    suspected_service: 'orders',
    parent_incident_id: null,
    new_evidence_banner: false,
    started_at: '2026-11-21T14:02:10Z',
    opened_at: '2026-11-21T14:02:31Z',
    finding_at: '2026-11-21T14:02:58Z',
    decided_at: null,
    mitigated_at: null,
    resolved_at: null,
  },
  alerts: [],
  investigation: null,
  finding: {
    id: 'f-1',
    investigation_id: 'inv-1',
    body: {
      suspected_service: 'orders',
      fault_type: 'bad_release',
      hypothesis: 'Orders 1.4.0 introduced KeyError on line_items right after deploy.',
      evidence: [
        { evidence_id: 'ev-01', claim: 'orders error_ratio 0.4% -> 38%' },
        { evidence_id: 'ev-02', claim: 'orders 1.3.0 -> 1.4.0 deployed' },
      ],
      confidence: 'high',
      proposed_action: {
        type: 'rollback',
        service: 'orders',
        rationale: 'Errors began 18s after the 1.4.0 rollout.',
      },
    },
    suspected_service: 'orders',
    fault_type: 'bad_release',
    confidence: 'high',
    action_type: 'rollback',
    action_service: 'orders',
    limit_reached: false,
    guardrail_notes: [],
    created_at: '2026-11-21T14:02:58Z',
  },
  action_request: {
    id: 'act-1',
    action: 'rollback',
    service: 'orders',
    from_version: '3f9a2c1',
    status: 'proposed',
    decided_by: null,
    decision_reason: null,
    decided_at: null,
    expires_at: '2026-11-21T14:17:58Z',
    executed_at: null,
    result: null,
  },
  postmortem_available: false,
  questions: [],
};

describe('T35 · FindingCard role enforcement & decision reason requirement (FR-IC-01, FR-IC-04)', () => {
  it('T35: responder sees Approve/Reject disabled until reason >= 3 chars', () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <FindingCard
          detail={sampleDetail}
          user={{ id: 'u1', email: 'resp@sentinel.local', role: 'responder' }}
          onOpenEvidence={vi.fn()}
        />
      </QueryClientProvider>
    );
    const approveBtn = screen.getByRole('button', { name: 'Approve' });
    const rejectBtn = screen.getByRole('button', { name: 'Reject' });
    const reasonInput = screen.getByPlaceholderText(/Reason \(required/i);

    // Empty or too-short (<3 chars) reason keeps both decision buttons disabled
    expect(approveBtn).toBeDisabled();
    expect(rejectBtn).toBeDisabled();
    fireEvent.change(reasonInput, { target: { value: 'no' } });
    expect(approveBtn).toBeDisabled();
    expect(rejectBtn).toBeDisabled();

    // Valid reason (>=3 chars) enables Approve and Reject
    fireEvent.change(reasonInput, { target: { value: 'Confirmed regression after 1.4.0' } });
    expect(approveBtn).toBeEnabled();
    expect(rejectBtn).toBeEnabled();
    expect(screen.getByText('ev-01')).toBeInTheDocument();
  });

  it('T35: hides Approve and Reject buttons for viewer role', () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <FindingCard
          detail={sampleDetail}
          user={{ id: 'u2', email: 'viewer@sentinel.local', role: 'viewer' }}
          onOpenEvidence={vi.fn()}
        />
      </QueryClientProvider>
    );
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument();
    expect(screen.getByText(/read-only, no buttons/i)).toBeInTheDocument();
  });
});
