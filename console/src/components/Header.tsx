import { Link } from 'react-router-dom';
import type { User } from '../types';

interface HeaderProps {
  user: User;
  onLogout: () => void;
}

export function Header({ user, onLogout }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <Link to="/" className="brand">
          Sentinel
        </Link>
        <Link to="/" className="nav-link">
          Incidents
        </Link>
      </div>
      <div className="topbar-right">
        <span>
          {user.email} · <strong>{user.role}</strong>
        </span>
        <button className="btn" onClick={onLogout}>
          Sign out
        </button>
      </div>
    </header>
  );
}
