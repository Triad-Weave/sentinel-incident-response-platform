import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type {
  IncidentDetailResponse,
  IncidentEventItem,
  IncidentListItem,
  InvestigationStepDetail,
  User,
} from '../types';

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api/v1${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data?.detail) detail = String(data.detail);
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  me: () => apiFetch<User>('/me'),
  login: (email: string, password: string) =>
    apiFetch<User>('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  logout: () => apiFetch<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  listIncidents: (params: { status?: string; service?: string }) => {
    const qs = new URLSearchParams();
    if (params.status) qs.set('status', params.status);
    if (params.service) qs.set('service', params.service);
    return apiFetch<{ items: IncidentListItem[]; next_cursor: string | null }>(
      `/incidents?${qs.toString()}`
    );
  },
  getIncident: (id: string) => apiFetch<IncidentDetailResponse>(`/incidents/${id}`),
  getEvents: (id: string, afterSeq = 0) =>
    apiFetch<{ events: IncidentEventItem[] }>(`/incidents/${id}/events?after_seq=${afterSeq}`),
  getStep: (investigationId: string, seq: number) =>
    apiFetch<InvestigationStepDetail>(`/investigations/${investigationId}/steps/${seq}`),
  approveAction: (actionId: string, reason: string) =>
    apiFetch<Record<string, unknown>>(`/actions/${actionId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  rejectAction: (actionId: string, reason: string) =>
    apiFetch<Record<string, unknown>>(`/actions/${actionId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  retryIncident: (incidentId: string) =>
    apiFetch<Record<string, unknown>>(`/incidents/${incidentId}/retry`, { method: 'POST' }),
  resolveIncident: (incidentId: string) =>
    apiFetch<Record<string, unknown>>(`/incidents/${incidentId}/resolve`, { method: 'POST' }),
  askQuestion: (incidentId: string, question: string) =>
    apiFetch<Record<string, unknown>>(`/incidents/${incidentId}/questions`, {
      method: 'POST',
      body: JSON.stringify({ question }),
    }),
};

/** Live SSE subscription for the incident list screen (FR-IC-02). */
export function useIncidentListStream() {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    const es = new EventSource('/api/v1/incidents/stream', { withCredentials: true });
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener('incident.updated', () => {
      queryClient.invalidateQueries({ queryKey: ['incidents'] });
    });
    return () => es.close();
  }, [queryClient]);

  return connected;
}

const STREAM_EVENT_TYPES = [
  'incident.opened',
  'alert.attached',
  'alert.resolved',
  'investigation.started',
  'step.recorded',
  'investigation.limit_reached',
  'investigation.failed',
  'finding.posted',
  'action.proposed',
  'action.approved',
  'action.rejected',
  'action.executing',
  'action.succeeded',
  'action.failed',
  'verification.passed',
  'verification.failed',
  'incident.status_changed',
  'postmortem.drafted',
];

/** Live SSE timeline stream with deduplication by `seq` (FR-IC-03, ADR-004). */
export function useIncidentStream(incidentId: string) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<IncidentEventItem[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let active = true;
    const seen = new Set<number>();

    const appendEvent = (ev: IncidentEventItem) => {
      if (seen.has(ev.seq)) return;
      seen.add(ev.seq);
      setEvents((prev) => [...prev, ev].sort((a, b) => a.seq - b.seq));
    };

    api.getEvents(incidentId, 0).then((res) => {
      if (!active) return;
      for (const ev of res.events) appendEvent(ev);
    }).catch(() => {});

    if (typeof EventSource === 'undefined') {
      return () => {
        active = false;
      };
    }

    const es = new EventSource(`/api/v1/incidents/${incidentId}/stream`, {
      withCredentials: true,
    });
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);

    const onEvent = (evt: MessageEvent) => {
      try {
        const parsed = JSON.parse(evt.data) as IncidentEventItem;
        appendEvent(parsed);
        if (parsed.type !== 'step.recorded') {
          queryClient.invalidateQueries({ queryKey: ['incident', incidentId] });
        }
      } catch {
        // ignore malformed frame
      }
    };

    for (const evType of STREAM_EVENT_TYPES) {
      es.addEventListener(evType, onEvent as EventListener);
    }

    return () => {
      active = false;
      es.close();
    };
  }, [incidentId, queryClient]);

  return { events, connected };
}
