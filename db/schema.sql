PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS VENUE (
    venue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    address TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS EVENT (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES VENUE(venue_id),
    event_code TEXT NOT NULL UNIQUE,
    event_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    start_date TEXT NOT NULL CHECK (
        start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    end_date TEXT NOT NULL CHECK (
        end_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    description TEXT,
    CHECK (start_date <= end_date)
);

CREATE TABLE IF NOT EXISTS BOOTH (
    booth_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES EVENT(event_id),
    booth_code TEXT NOT NULL,
    location TEXT,
    status TEXT NOT NULL CHECK (status IN ('在用', '停用')),
    UNIQUE (event_id, booth_code)
);

CREATE TABLE IF NOT EXISTS SALESPERSON (
    salesperson_id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT,
    status TEXT NOT NULL CHECK (status IN ('在职', '停用')),
    hired_at TEXT NOT NULL CHECK (
        hired_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    )
);

CREATE TABLE IF NOT EXISTS SHIFT (
    shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
    salesperson_id INTEGER NOT NULL REFERENCES SALESPERSON(salesperson_id),
    booth_id INTEGER NOT NULL REFERENCES BOOTH(booth_id),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('未开始', '进行中', '已结束', '已结算')),
    CHECK (start_time < end_time)
);

CREATE TABLE IF NOT EXISTS PRODUCT (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    min_price_cents INTEGER NOT NULL CHECK (min_price_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('在售', '停用')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS APP_USER (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('合伙人', '销售顾问')),
    salesperson_id INTEGER REFERENCES SALESPERSON(salesperson_id),
    status TEXT NOT NULL CHECK (status IN ('启用', '停用')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    UNIQUE (salesperson_id)
);

CREATE TABLE IF NOT EXISTS IMPORT_BATCH (
    import_batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    imported_by INTEGER NOT NULL REFERENCES APP_USER(user_id),
    imported_at TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    success_count INTEGER NOT NULL CHECK (success_count >= 0),
    error_count INTEGER NOT NULL CHECK (error_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('成功', '部分成功', '失败')),
    note TEXT,
    CHECK (row_count = success_count + error_count)
);

CREATE TABLE IF NOT EXISTS SALE (
    sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL REFERENCES SHIFT(shift_id),
    import_batch_id INTEGER REFERENCES IMPORT_BATCH(import_batch_id),
    sale_time TEXT NOT NULL,
    client_txn_id TEXT NOT NULL UNIQUE,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('已登记', '待计佣', '已计佣', '作废')),
    void_reason TEXT,
    void_by INTEGER REFERENCES APP_USER(user_id),
    void_at TEXT,
    CHECK (
        (status = '作废' AND void_reason IS NOT NULL AND void_by IS NOT NULL AND void_at IS NOT NULL)
        OR
        (status <> '作废' AND void_reason IS NULL AND void_by IS NULL AND void_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS SALE_ITEM (
    sale_id INTEGER NOT NULL REFERENCES SALE(sale_id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL CHECK (line_no > 0),
    product_id INTEGER NOT NULL REFERENCES PRODUCT(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    final_unit_price_cents INTEGER NOT NULL CHECK (final_unit_price_cents >= 0),
    line_cents INTEGER NOT NULL CHECK (line_cents >= 0),
    PRIMARY KEY (sale_id, line_no),
    CHECK (line_cents = quantity * final_unit_price_cents)
);

CREATE TABLE IF NOT EXISTS COMMISSION_RULE (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    dimension TEXT NOT NULL CHECK (dimension IN ('PRODUCT', 'EVENT_TYPE', 'SALESPERSON', 'GLOBAL')),
    event_type_value TEXT,
    calc_method TEXT NOT NULL CHECK (calc_method IN ('按比例', '按件固定额')),
    rate REAL,
    fixed_per_unit_cents INTEGER,
    priority INTEGER NOT NULL DEFAULT 0,
    effective_from TEXT NOT NULL CHECK (
        effective_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    effective_to TEXT,
    status TEXT NOT NULL CHECK (status IN ('草稿', '有效', '停用')),
    created_by INTEGER NOT NULL REFERENCES APP_USER(user_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    UNIQUE (rule_code, version_no),
    CHECK (
        effective_to IS NULL
        OR (
            effective_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            AND effective_from <= effective_to
        )
    ),
    CHECK (
        (calc_method = '按比例' AND rate IS NOT NULL AND rate >= 0 AND rate <= 1
            AND fixed_per_unit_cents IS NULL)
        OR
        (calc_method = '按件固定额' AND rate IS NULL
            AND fixed_per_unit_cents IS NOT NULL AND fixed_per_unit_cents >= 0)
    ),
    CHECK (
        (dimension = 'EVENT_TYPE' AND event_type_value IS NOT NULL AND length(trim(event_type_value)) > 0)
        OR
        (dimension <> 'EVENT_TYPE' AND event_type_value IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS PRODUCT_COMMISSION_RULE (
    product_id INTEGER NOT NULL REFERENCES PRODUCT(product_id),
    rule_id INTEGER NOT NULL REFERENCES COMMISSION_RULE(rule_id),
    PRIMARY KEY (product_id, rule_id)
);

CREATE TABLE IF NOT EXISTS SALESPERSON_COMMISSION_RULE (
    salesperson_id INTEGER NOT NULL REFERENCES SALESPERSON(salesperson_id),
    rule_id INTEGER NOT NULL REFERENCES COMMISSION_RULE(rule_id),
    PRIMARY KEY (salesperson_id, rule_id)
);

CREATE TABLE IF NOT EXISTS COMMISSION_SETTLEMENT (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_no TEXT NOT NULL UNIQUE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    scope_desc TEXT,
    status TEXT NOT NULL CHECK (status IN ('草稿', '已确认')),
    confirmed_by INTEGER REFERENCES APP_USER(user_id),
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    CHECK (period_start <= period_end),
    CHECK (
        (status = '草稿' AND confirmed_by IS NULL AND confirmed_at IS NULL)
        OR
        (status = '已确认' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS COMMISSION_ENTRY (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    rule_id INTEGER NOT NULL REFERENCES COMMISSION_RULE(rule_id),
    settlement_id INTEGER REFERENCES COMMISSION_SETTLEMENT(settlement_id),
    base_cents INTEGER NOT NULL CHECK (base_cents >= 0),
    commission_cents INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('待结算', '已结算', '已冲减')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    UNIQUE (sale_id, line_no),
    FOREIGN KEY (sale_id, line_no) REFERENCES SALE_ITEM(sale_id, line_no)
);

CREATE TABLE IF NOT EXISTS COMMISSION_SETTLEMENT_LINE (
    settlement_id INTEGER NOT NULL REFERENCES COMMISSION_SETTLEMENT(settlement_id),
    salesperson_id INTEGER NOT NULL REFERENCES SALESPERSON(salesperson_id),
    net_sales_cents INTEGER NOT NULL CHECK (net_sales_cents >= 0),
    commission_total_cents INTEGER NOT NULL,
    pending_cents INTEGER NOT NULL CHECK (pending_cents >= 0),
    adjustment_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (settlement_id, salesperson_id)
);

CREATE INDEX IF NOT EXISTS idx_event_venue_id ON EVENT(venue_id);
CREATE INDEX IF NOT EXISTS idx_booth_event_id ON BOOTH(event_id);
CREATE INDEX IF NOT EXISTS idx_shift_salesperson_start_time ON SHIFT(salesperson_id, start_time);
CREATE INDEX IF NOT EXISTS idx_shift_booth_id ON SHIFT(booth_id);
CREATE INDEX IF NOT EXISTS idx_product_status ON PRODUCT(status);
CREATE INDEX IF NOT EXISTS idx_app_user_salesperson_id ON APP_USER(salesperson_id);
CREATE INDEX IF NOT EXISTS idx_import_batch_imported_by ON IMPORT_BATCH(imported_by);
CREATE INDEX IF NOT EXISTS idx_import_batch_imported_at ON IMPORT_BATCH(imported_at);
CREATE INDEX IF NOT EXISTS idx_sale_shift_id ON SALE(shift_id);
CREATE INDEX IF NOT EXISTS idx_sale_import_batch_id ON SALE(import_batch_id);
CREATE INDEX IF NOT EXISTS idx_sale_sale_time ON SALE(sale_time);
CREATE INDEX IF NOT EXISTS idx_sale_status_sale_time ON SALE(status, sale_time);
CREATE INDEX IF NOT EXISTS idx_sale_void_by ON SALE(void_by);
CREATE INDEX IF NOT EXISTS idx_sale_item_product ON SALE_ITEM(product_id, sale_id, line_no);
CREATE INDEX IF NOT EXISTS idx_commission_rule_match
    ON COMMISSION_RULE(status, dimension, effective_from, effective_to, priority DESC, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_commission_rule_created_by ON COMMISSION_RULE(created_by);
CREATE INDEX IF NOT EXISTS idx_product_commission_rule_rule ON PRODUCT_COMMISSION_RULE(rule_id);
CREATE INDEX IF NOT EXISTS idx_salesperson_commission_rule_rule ON SALESPERSON_COMMISSION_RULE(rule_id);
CREATE INDEX IF NOT EXISTS idx_commission_entry_settlement_status
    ON COMMISSION_ENTRY(settlement_id, status);
CREATE INDEX IF NOT EXISTS idx_commission_entry_rule_id ON COMMISSION_ENTRY(rule_id);
CREATE INDEX IF NOT EXISTS idx_settlement_period_status
    ON COMMISSION_SETTLEMENT(period_start, period_end, status);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmed_by ON COMMISSION_SETTLEMENT(confirmed_by);
CREATE INDEX IF NOT EXISTS idx_settlement_line_salesperson
    ON COMMISSION_SETTLEMENT_LINE(salesperson_id);