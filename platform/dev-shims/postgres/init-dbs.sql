-- Platform Postgres initialisation for Sentinel (4 databases + least-privilege roles, ADR-010 & NFR-04).
-- Ground-truth database `chaos` revokes PUBLIC connect so no Sentinel role can ever reach it (FR-CH-03).

-- 1. Roles
CREATE ROLE orders_app WITH LOGIN PASSWORD 'orders_dev_pw';
CREATE ROLE payments_app WITH LOGIN PASSWORD 'payments_dev_pw';
CREATE ROLE chaos_rw WITH LOGIN PASSWORD 'chaos_dev_pw';
CREATE ROLE sentinel_api WITH LOGIN PASSWORD 'sentinel_api_dev_pw';
CREATE ROLE sentinel_worker WITH LOGIN PASSWORD 'sentinel_worker_dev_pw';
CREATE ROLE sentinel_actuator WITH LOGIN PASSWORD 'sentinel_actuator_dev_pw';

-- 2. Databases
CREATE DATABASE orders OWNER orders_app;
CREATE DATABASE payments OWNER payments_app;
CREATE DATABASE sentinel;
CREATE DATABASE chaos OWNER chaos_rw;

-- 3. Strict database connect isolation
REVOKE CONNECT ON DATABASE orders FROM PUBLIC;
GRANT CONNECT ON DATABASE orders TO orders_app;

REVOKE CONNECT ON DATABASE payments FROM PUBLIC;
GRANT CONNECT ON DATABASE payments TO payments_app;

REVOKE CONNECT ON DATABASE chaos FROM PUBLIC;
GRANT CONNECT ON DATABASE chaos TO chaos_rw;

REVOKE CONNECT ON DATABASE sentinel FROM PUBLIC;
GRANT CONNECT ON DATABASE sentinel TO sentinel_api, sentinel_worker, sentinel_actuator;

-- 4. Schema grants inside `sentinel` (tables created by Alembic migration or sentinel-api)
\connect sentinel
GRANT USAGE, CREATE ON SCHEMA public TO sentinel_api;
GRANT USAGE ON SCHEMA public TO sentinel_worker, sentinel_actuator;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO sentinel_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sentinel_api, sentinel_worker, sentinel_actuator;
ALTER DEFAULT PRIVILEGES FOR ROLE sentinel_api IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sentinel_api, sentinel_worker, sentinel_actuator;
