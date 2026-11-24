import { FormEvent, useState } from 'react';
import { api } from '../api/client';
import type { User } from '../types';

export function LoginPage({ onLoggedIn }: { onLoggedIn: (u: User) => void }) {
  const [email, setEmail] = useState('responder@sentinel.local');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const user = await api.login(email, password);
      onLoggedIn(user);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-shell">
      <div className="card login-card">
        <h2 style={{ marginTop: 0 }}>Sentinel Console</h2>
        <p className="incident-meta">Sign in with your local Responder or Viewer account.</p>
        <form onSubmit={handleSubmit}>
          <label className="field">
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label className="field">
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <p className="error-text">{error}</p>}
          <button
            className="btn btn-primary"
            type="submit"
            style={{ width: '100%' }}
            disabled={submitting}
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
