-- Initial schema. See "Datamodell" in the spec and docs/spec-logg.md.
-- Money is integer öre, times are ISO-8601 UTC text ("2026-10-25T14:00:00Z"),
-- column names are ASCII (pris_öre -> pris_ore), enum values follow the spec.

CREATE TABLE wishlist (
    id               INTEGER PRIMARY KEY,
    typ              TEXT NOT NULL CHECK (typ IN ('amazon', 'lokal')),
    namn             TEXT NOT NULL,
    url              TEXT,
    list_id          TEXT UNIQUE,  -- NULL for lokal
    standard_procent INTEGER CHECK (standard_procent BETWEEN 1 AND 99),
    standard_aktiv   INTEGER NOT NULL DEFAULT 0 CHECK (standard_aktiv IN (0, 1)),
    status           TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'blockerad', 'privat', 'fel')),
    senast_synkad    TEXT,
    skapad           TEXT NOT NULL
);

CREATE TABLE product (
    asin               TEXT PRIMARY KEY CHECK (length(asin) = 10),
    titel              TEXT NOT NULL DEFAULT '',
    bild_url           TEXT,
    url                TEXT,
    aktiv              INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0, 1)),
    forsta_sedd        TEXT NOT NULL,
    -- Prime cache
    prime_status       TEXT NOT NULL DEFAULT 'okänd' CHECK (prime_status IN ('prime', 'inte_prime', 'okänd')),
    prime_kontrollerad TEXT,
    -- Last known state, compared against each round to decide what changed
    senaste_status     TEXT CHECK (senaste_status IN ('pris', 'ej_i_lager', 'saknar_pris')),
    senaste_pris_ore   INTEGER,
    senast_last        TEXT
);
-- Background Prime refresh picks the oldest checks first
CREATE INDEX product_prime_kontrollerad ON product (prime_kontrollerad);

CREATE TABLE wishlist_product (
    wishlist_id INTEGER NOT NULL REFERENCES wishlist (id) ON DELETE CASCADE,
    asin        TEXT NOT NULL REFERENCES product (asin),
    tillagd     TEXT NOT NULL,
    borttagen   TEXT,  -- NULL = still in the list
    PRIMARY KEY (wishlist_id, asin)
);
CREATE INDEX wishlist_product_asin ON wishlist_product (asin);

CREATE TABLE run (
    id               INTEGER PRIMARY KEY,
    omgang           INTEGER NOT NULL,  -- shared by the per-list runs of one execution
    typ              TEXT NOT NULL CHECK (typ IN ('schemalagd', 'manuell', 'probe')),
    wishlist_id      INTEGER REFERENCES wishlist (id) ON DELETE SET NULL,
    list_namn        TEXT,  -- snapshot, survives removal of the list
    list_id          TEXT,
    start            TEXT NOT NULL,
    slut             TEXT,
    sidor            INTEGER NOT NULL DEFAULT 0,
    lasta            INTEGER NOT NULL DEFAULT 0,
    prisandrade      INTEGER NOT NULL DEFAULT 0,
    nya              INTEGER NOT NULL DEFAULT 0,
    borttagna        INTEGER NOT NULL DEFAULT 0,
    produktsidor     INTEGER NOT NULL DEFAULT 0,
    prime_andrade    INTEGER NOT NULL DEFAULT 0,
    larm_skickade    INTEGER NOT NULL DEFAULT 0,
    larm_misslyckade INTEGER NOT NULL DEFAULT 0,
    fel              INTEGER NOT NULL DEFAULT 0,
    captcha          TEXT CHECK (captcha IN ('listsida', 'produktsida')),  -- NULL = no CAPTCHA
    resultat         TEXT CHECK (resultat IN ('ok', 'delvis', 'fel'))  -- NULL while running
);
CREATE INDEX run_start ON run (start);
CREATE INDEX run_omgang ON run (omgang);

CREATE TABLE price_point (
    id        INTEGER PRIMARY KEY,
    asin      TEXT NOT NULL REFERENCES product (asin),
    tidpunkt  TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('pris', 'ej_i_lager', 'saknar_pris')),
    pris_ore  INTEGER CHECK (pris_ore > 0),
    prime     TEXT NOT NULL CHECK (prime IN ('prime', 'inte_prime', 'okänd')),  -- cache at measurement
    kalla     TEXT NOT NULL CHECK (kalla IN ('lista', 'produktsida')),
    misstankt INTEGER NOT NULL DEFAULT 0 CHECK (misstankt IN (0, 1)),
    run_id    INTEGER REFERENCES run (id) ON DELETE SET NULL,
    CHECK ((status = 'pris') = (pris_ore IS NOT NULL))
);
CREATE INDEX price_point_asin_tidpunkt ON price_point (asin, tidpunkt);

CREATE TABLE alert_rule (
    asin                  TEXT PRIMARY KEY REFERENCES product (asin),  -- no row = inherit list default
    mal_pris_ore          INTEGER CHECK (mal_pris_ore > 0),
    mal_procent           INTEGER CHECK (mal_procent BETWEEN 1 AND 99),
    arver_standard        INTEGER NOT NULL DEFAULT 1 CHECK (arver_standard IN (0, 1)),
    lagsta_nagonsin_notis INTEGER NOT NULL DEFAULT 0 CHECK (lagsta_nagonsin_notis IN (0, 1)),
    larm_aktivt           INTEGER NOT NULL DEFAULT 1 CHECK (larm_aktivt IN (0, 1)),
    senaste_larm_pris     INTEGER,  -- NULL = suppression reset
    senaste_larm_tid      TEXT
);

-- Only written on change; notification events (larm_*, drift_notis, test_notis) are
-- the notification log and outlive their run (run_id becomes NULL).
CREATE TABLE event (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER REFERENCES run (id) ON DELETE SET NULL,
    tid     TEXT NOT NULL,
    typ     TEXT NOT NULL CHECK (typ IN (
                'pris_ändrat', 'ny_produkt', 'borttagen_produkt', 'ej_i_lager', 'åter_i_lager',
                'prime_ändrad', 'produktsida_hämtad', 'larm_skickat', 'larm_misslyckades',
                'saknar_pris', 'http_fel', 'parsningsfel', 'captcha', 'drift_notis', 'test_notis')),
    asin    TEXT,  -- no FK: the log must never block cleanup of core data
    fore    TEXT,  -- raw values (öre, status names); the GUI formats them
    efter   TEXT,
    text    TEXT
);
CREATE INDEX event_run_typ ON event (run_id, typ);
CREATE INDEX event_asin_tid ON event (asin, tid);
CREATE INDEX event_tid ON event (tid);

CREATE TABLE settings (
    nyckel TEXT PRIMARY KEY,
    varde  TEXT NOT NULL
);
INSERT INTO settings (nyckel, varde) VALUES
    ('prime_ttl', '604800'),               -- seconds (7 days)
    ('prime_fresh_for_alert', '86400'),    -- seconds (24 h)
    ('max_product_pages_per_round', '10'),
    ('logg_lagringstid_dagar', '30'),
    ('notislogg_lagringstid_dagar', '365');
