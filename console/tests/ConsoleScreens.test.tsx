import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { Header } from '../src/components/Header';
import { StatusBadge } from '../src/components/StatusBadge';
import { IncidentDetailPage } from '../src/pages/IncidentDetailPage';
import { LoginPage } from '../src/pages/LoginPage';
import { api } from '../src/api/client';
import type { IncidentDetailResponse } from '../src/types';

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, Array<(e: MessageEvent) => void>> = {};
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    this.listeners[type] = this.listeners[type] ?? [];
    this.listeners[type].push(cb);
  }

  emit(type: string, data: unknown) {
    const evt = { data: JSON.stringify(data) } as MessageEvent;
    for (const cb of this.listeners[type] ?? []) cb(evt);
  }

  close() {
    this.closed = true;
  }
}

const detailFixture: IncidentDetailResponse = {
  incident: {
    id: 'inc-sse-1',
    short_id: 'INC-0099',
    status: 'Investigating',
    title: 'Orders 5xx elevated',
    alert_service: 'orders',
    suspected_service: null,
    parent_incident_id: null,
    new_evidence_banner: false,
    started_at: '2026-11-22T10:00:00Z',
    opened_at: '2026-11-22T10:00:05Z',
    finding_at: null,
    decided_at: null,
    mitigated_at: null,
    resolved_at: null,
  },
  alerts: [],
  investigation: null,
  finding: null,
  action_request: null,
  postmortem_available: false,
  questions: [],
};

afterEach(() => {
  vi.restoreAllMocks();
  MockEventSource.instances = [];
  delete (globalThis as Record<string, unknown>).EventSource;
});

describe('T35 · Console screens, statuses, and SSE timeline append (FR-IC-01..04)', () => {
  it('renders Sign-in screen with Email, Password, and Sign in button', () => {
    render(<LoginPage onLoggedIn={() => {}} />);
    expect(screen.getByText('Sentinel Console')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('renders Header with signed-in user role and Incidents navigation', () => {
    render(
      <MemoryRouter>
        <Header
          user={{ id: 'u1', email: 'responder@sentinel.local', role: 'responder' }}
          onLogout={() => {}}
        />
      </MemoryRouter>
    );
    expect(screen.getByText('Sentinel')).toBeInTheDocument();
    expect(screen.getByText('Incidents')).toBeInTheDocument();
    expect(screen.getByText('responder')).toBeInTheDocument();
  });

  it('T35: renders all 8 incident lifecycle statuses cleanly in StatusBadge', () => {
    const statuses = [
      'Investigating',
      'Investigation failed',
      'Awaiting decision',
      'Action in progress',
      'Monitoring',
      'Mitigated',
      'Rejected',
      'Resolved',
    ];
    for (const s of statuses) {
      const { unmount } = render(<StatusBadge status={s} />);
      expect(screen.getByText(s)).toBeInTheDocument();
      unmount();
    }
  });

  it('T35: timeline appends incoming SSE events in order without reload and deduplicates by seq', async () => {
    (globalThis as Record<string, unknown>).EventSource = MockEventSource;
    vi.spyOn(api, 'getIncident').mockResolvedValue(detailFixture);
    vi.spyOn(api, 'getEvents').mockResolvedValue({
      events: [
        {
          id: 'e1',
          incident_id: 'inc-sse-1',
          seq: 1,
          type: 'incident.opened',
          actor: 'alertmanager',
          payload: { summary: 'Alert ServiceHighErrorRate opened incident' },
          created_at: '2026-11-22T10:00:05Z',
        },
      ],
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/incidents/inc-sse-1']}>
          <Routes>
            <Route
              path="/incidents/:id"
              element={
                <IncidentDetailPage
                  user={{ id: 'u2', email: 'viewer@sentinel.local', role: 'viewer' }}
                />
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('Alert ServiceHighErrorRate opened incident')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: 'Resolve' })).not.toBeInTheDocument();
    expect(MockEventSource.instances.length).toBe(1);

    const es = MockEventSource.instances[0];
    act(() => {
      es.onopen?.();
      es.emit('step.recorded', {
        id: 'e2',
        incident_id: 'inc-sse-1',
        seq: 2,
        type: 'step.recorded',
        actor: 'sentinel-worker',
        payload: {
          seq: 1,
          evidence_id: 'ev-01',
          tool_name: 'get_golden_signals',
          summary: 'orders error_ratio jumped to 42%',
        },
        created_at: '2026-11-22T10:00:12Z',
      });
      // Re-emit duplicate seq=2 to prove deduplication
      es.emit('step.recorded', {
        id: 'e2',
        incident_id: 'inc-sse-1',
        seq: 2,
        type: 'step.recorded',
        actor: 'sentinel-worker',
        payload: {
          seq: 1,
          evidence_id: 'ev-01',
          tool_name: 'get_golden_signals',
          summary: 'orders error_ratio jumped to 42%',
        },
        created_at: '2026-11-22T10:00:12Z',
      });
    });

    await waitFor(() => {
      expect(screen.getByText('orders error_ratio jumped to 42%')).toBeInTheDocument();
    });
    expect(screen.getAllByText('orders error_ratio jumped to 42%')).toHaveLength(1);
    expect(screen.getByText('ev-01')).toBeInTheDocument();
  });
});
