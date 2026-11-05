CREATE USER semora_reader WITH PASSWORD 'semora_reader';
CREATE USER semora_api WITH PASSWORD 'semora_api';
CREATE USER semora_audit_exporter WITH PASSWORD 'semora_audit_exporter';

CREATE SCHEMA semora_meta AUTHORIZATION semora_api;
CREATE TABLE semora_meta.sessions (
    id VARCHAR(64) PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    domain VARCHAR(100) NOT NULL DEFAULT 'retail_credit',
    owner_subject VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE semora_meta.runs (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL REFERENCES semora_meta.sessions(id),
    status VARCHAR(32) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE semora_meta.audit_events (
    id VARCHAR(64) PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    sequence INTEGER NOT NULL,
    event_hash CHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE TABLE semora_meta.datasources (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    type VARCHAR(32) NOT NULL,
    secret_ref VARCHAR(200) NOT NULL,
    status VARCHAR(32) NOT NULL,
    access VARCHAR(32) NOT NULL,
    objects INTEGER NOT NULL DEFAULT 0,
    last_scan_at TIMESTAMPTZ,
    scan_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE semora_meta.system_settings (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    payload JSONB NOT NULL,
    version INTEGER NOT NULL,
    policy_version VARCHAR(96) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by VARCHAR(200) NOT NULL
);
GRANT USAGE ON SCHEMA semora_meta TO semora_api;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA semora_meta TO semora_api;

CREATE TABLE branches (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(20) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL
);

CREATE TABLE customers (
    id BIGSERIAL PRIMARY KEY,
    customer_no VARCHAR(32) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    mobile VARCHAR(32) NOT NULL,
    id_no VARCHAR(64) NOT NULL,
    branch_id BIGINT NOT NULL REFERENCES branches(id)
);

CREATE TABLE loans (
    id BIGSERIAL PRIMARY KEY,
    loan_no VARCHAR(32) NOT NULL UNIQUE,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    branch_id BIGINT NOT NULL REFERENCES branches(id),
    outstanding_balance NUMERIC(18,2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    as_of_date DATE NOT NULL
);

CREATE TABLE payments (
    id BIGSERIAL PRIMARY KEY,
    loan_id BIGINT NOT NULL REFERENCES loans(id),
    days_overdue INTEGER NOT NULL DEFAULT 0,
    overdue_amount NUMERIC(18,2) NOT NULL DEFAULT 0,
    as_of_date DATE NOT NULL
);

INSERT INTO branches(code, name) VALUES
('SH', '上海分行'), ('SZ', '深圳分行'), ('HZ', '杭州分行');

INSERT INTO customers(customer_no, name, mobile, id_no, branch_id) VALUES
('C10001', '张明', '13800001001', '310***********001', 1),
('C10002', '李琳', '13800001002', '440***********002', 2),
('C10003', '周凯', '13800001003', '330***********003', 3),
('C10004', '王悦', '13800001004', '310***********004', 1),
('C10005', '陈晨', '13800001005', '440***********005', 2),
('C10006', '赵宁', '13800001006', '330***********006', 3);

INSERT INTO loans(loan_no, customer_id, branch_id, outstanding_balance, status, as_of_date) VALUES
('L20260001', 1, 1, 1250000.00, 'active', CURRENT_DATE),
('L20260002', 2, 2, 980000.00, 'active', CURRENT_DATE),
('L20260003', 3, 3, 760000.00, 'active', CURRENT_DATE),
('L20260004', 4, 1, 540000.00, 'active', CURRENT_DATE),
('L20260005', 5, 2, 430000.00, 'active', CURRENT_DATE),
('L20260006', 6, 3, 310000.00, 'active', CURRENT_DATE);

INSERT INTO payments(loan_id, days_overdue, overdue_amount, as_of_date) VALUES
(1, 0, 0, CURRENT_DATE), (2, 97, 72000, CURRENT_DATE),
(3, 12, 16000, CURRENT_DATE), (4, 110, 84000, CURRENT_DATE),
(5, 0, 0, CURRENT_DATE), (6, 46, 22000, CURRENT_DATE);

GRANT CONNECT ON DATABASE semora_business TO semora_reader;
GRANT CONNECT ON DATABASE semora_business TO semora_api;
GRANT USAGE ON SCHEMA public TO semora_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO semora_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO semora_reader;
ALTER USER semora_reader SET default_transaction_read_only = on;

