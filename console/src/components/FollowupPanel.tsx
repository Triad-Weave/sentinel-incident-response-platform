import { FormEvent, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { IncidentDetailResponse, User } from '../types';

export function FollowupPanel({ detail, user }: { detail: IncidentDetailResponse; user: User }) {
  const queryClient = useQueryClient();
  const [question, setQuestion] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const askMutation = useMutation({
    mutationFn: () => api.askQuestion(detail.incident.id, question.trim()),
    onSuccess: () => {
      setQuestion('');
      setErr(null);
      queryClient.invalidateQueries({ queryKey: ['incident', detail.incident.id] });
    },
    onError: (e: Error) => setErr(e.message),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (question.trim().length < 3) return;
    askMutation.mutate();
  };

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h4 style={{ marginTop: 0 }}>Follow-up Q&A</h4>
      {detail.questions.length === 0 ? (
        <p className="incident-meta">No follow-up questions asked yet.</p>
      ) : (
        detail.questions.map((q) => (
          <div key={q.id} style={{ marginBottom: 12, fontSize: 13 }}>
            <div>
              <strong>Q ({q.asked_by}):</strong> {q.question}
            </div>
            <div style={{ color: '#334155', marginTop: 4 }}>
              <strong>Sentinel:</strong> {q.answer}
            </div>
          </div>
        ))
      )}

      {user.role === 'responder' && (
        <form onSubmit={onSubmit} style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <input
            type="text"
            style={{ flex: 1 }}
            placeholder="Ask a follow-up question about this incident…"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <button
            className="btn btn-primary"
            type="submit"
            disabled={question.trim().length < 3 || askMutation.isPending}
          >
            Ask
          </button>
        </form>
      )}
      {err && <p className="error-text">{err}</p>}
    </div>
  );
}
