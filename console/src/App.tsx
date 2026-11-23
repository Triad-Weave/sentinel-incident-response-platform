import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api } from './api/client';
import { Header } from './components/Header';
import { IncidentDetailPage } from './pages/IncidentDetailPage';
import { IncidentListPage } from './pages/IncidentListPage';
import { LoginPage } from './pages/LoginPage';
import type { User } from './types';

const queryClient = new QueryClient();

function AppShell() {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    api
      .me()
      .then((u) => setUser(u))
      .catch(() => setUser(null))
      .finally(() => setChecked(true));
  }, []);

  if (!checked) return <div className="page">Checking session…</div>;
  if (!user) return <LoginPage onLoggedIn={setUser} />;

  const handleLogout = async () => {
    await api.logout();
    setUser(null);
  };

  return (
    <>
      <Header user={user} onLogout={handleLogout} />
      <Routes>
        <Route path="/" element={<IncidentListPage />} />
        <Route path="/incidents/:id" element={<IncidentDetailPage user={user} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppShell />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
