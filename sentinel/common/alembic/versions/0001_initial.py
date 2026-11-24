"""Initial Sentinel schema + pg_notify trigger + least-privilege DB role grants (NFR-04).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op
from sentinel_common.models import SentinelBase

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    SentinelBase.metadata.create_all(bind=bind)

    if bind.dialect.name == "postgresql":
        # Enforce at most one running investigation per incident (FR-AG-02, T14)
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_incident
                ON investigations (incident_id)
                WHERE status = 'running';
            """
        )

        # Append-only incident_events NOTIFY trigger (Architecture p.30-31)
        op.execute(
            """
            CREATE OR REPLACE FUNCTION notify_incident_event() RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('incident_events', NEW.incident_id::text || ':' || NEW.seq::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_notify_incident_event ON incident_events;
            CREATE TRIGGER trg_notify_incident_event
                AFTER INSERT ON incident_events
                FOR EACH ROW EXECUTE FUNCTION notify_incident_event();
            """
        )

        # Least-privilege table & column grants (Architecture p.20, 33-34)
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_worker') THEN
                    GRANT SELECT, UPDATE ON incidents TO sentinel_worker;
                    GRANT SELECT ON alerts TO sentinel_worker;
                    GRANT SELECT, INSERT, UPDATE ON investigations TO sentinel_worker;
                    GRANT SELECT, INSERT ON investigation_steps, llm_calls, findings, incident_events, incident_questions TO sentinel_worker;
                    GRANT SELECT, INSERT, UPDATE ON postmortems TO sentinel_worker;
                    GRANT SELECT, INSERT ON action_requests TO sentinel_worker;
                END IF;

                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sentinel_actuator') THEN
                    GRANT SELECT ON incidents, action_requests TO sentinel_actuator;
                    GRANT UPDATE (status) ON incidents TO sentinel_actuator;
                    GRANT UPDATE (status, claimed_at, executed_at, result) ON action_requests TO sentinel_actuator;
                    GRANT INSERT ON incident_events TO sentinel_actuator;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_notify_incident_event ON incident_events;")
        op.execute("DROP FUNCTION IF EXISTS notify_incident_event();")
    SentinelBase.metadata.drop_all(bind=bind)
