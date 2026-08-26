-- Analytics schema for D1. Identical in shape to the SQLite schema the
-- container uses, so the two deployments produce comparable data.
--
-- Apply with:
--   wrangler d1 migrations apply excel-formula-generator --local    (development)
--   wrangler d1 migrations apply excel-formula-generator --remote   (production)
--
-- What is stored, and what is deliberately not: the description a user typed,
-- the detected language, the matched function and whether a formula could be
-- built. NOT the contents of anyone's table, and not their column headers —
-- a header can name a client or a project. Column count and types are enough
-- to understand usage and carry no personal data.

CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    REAL    NOT NULL,
    language      TEXT,
    platform      TEXT,
    locale        TEXT,
    source        TEXT,            -- none | paste | xlsx | csv | described
    description   TEXT    NOT NULL,
    intent        TEXT,
    function      TEXT,
    status        TEXT,            -- filled | template | no_match
    column_count  INTEGER,
    column_types  TEXT             -- e.g. "text,text,number"
);

-- The unmet-needs panel filters on status and orders by recency; the popular
-- list groups by description. These two indexes serve both.
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
