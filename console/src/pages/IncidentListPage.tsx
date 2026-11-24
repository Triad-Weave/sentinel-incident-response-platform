import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, useIncidentListStream } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';

export function IncidentListPage() {
  const navigate = useNavigate();
  const [statusFilter, setStatusFilter] = useState('open');
  const [serviceFilter, setServiceFilter] = useState('all');
  const liveConnected = useIncidentListStream();

  const { data, isLoading, error } = useQuery({
    queryKey: ['incidents', statusFilter, serviceFilter],
    queryFn: () => api.listIncidents({ status: statusFilter, service: serviceFilter }),
    refetchInterval: 30_000,
  });

  return (
    <div className="page">
      <div className="card">
        <div className="filters-row">
          <div className="filter-group">
            <label>
              Status:{' '}
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="open">open</option>
                <option value="all">all</option>
                <option value="Investigating">Investigating</option>
                <option value="Awaiting decision">Awaiting decision</option>
                <option value="Action in progress">Action in progress</option>
                <option value="Mitigated">Mitigated</option>
                <option value="Investigation failed">Investigation failed</option>
                <option value="Resolved">Resolved</option>
              </select>
            </label>
            <label>
              Service:{' '}
              <select value={serviceFilter} onChange={(e) => setServiceFilter(e.target.value)}>
                <option value="all">all</option>
                <option value="orders">orders</option>
                <option value="payments">payments</option>
              </select>
            </label>
          </div>
          <span className="live-indicator">
            <span className={`dot ${liveConnected ? 'dot-green' : 'dot-gray'}`} />
            updates live
          </span>
        </div>

        {isLoading && <p className="incident-meta">Loading incidents…</p>}
        {error && <p className="error-text">{(error as Error).message}</p>}

        {data && (
          <table className="table">
            <thead>
              <tr>
                <th>Incident</th>
                <th>Status</th>
                <th>Service</th>
                <th>Started</th>
                <th>Finding</th>
              </tr>
            </thead>
            <tbody>
              {data.items.length === 0 ? (
                <tr>
                  <td colSpan={5} className="incident-meta">
                    No incidents match the current filter.
                  </td>
                </tr>
              ) : (
                data.items.map((inc) => (
                  <tr
                    key={inc.id}
                    className="clickable"
                    onClick={() => navigate(`/incidents/${inc.id}`)}
                  >
                    <td>
                      <strong>{inc.short_id}</strong>
                    </td>
                    <td>
                      <StatusBadge status={inc.status} />
                    </td>
                    <td>{inc.suspected_service ?? inc.alert_service}</td>
                    <td>{new Date(inc.started_at).toLocaleTimeString()}</td>
                    <td>
                      {inc.finding_summary
                        ? `${inc.finding_summary.fault_type}, ${inc.finding_summary.confidence}`
                        : inc.status === 'Investigation failed'
                          ? 'none yet, Retry'
                          : 'investigating…'}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
