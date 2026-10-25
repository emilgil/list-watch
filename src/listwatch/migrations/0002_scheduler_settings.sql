-- Scheduler settings (spec "Webbgränssnitt" > Inställningar; backoff in "Datainsamling").
INSERT OR IGNORE INTO settings (nyckel, varde) VALUES
    ('intervall_timmar', '6'),           -- lowest 1
    ('kampanj_intervall_timmar', '0'),   -- 0 = no campaign interval
    ('kampanj_slut', ''),                -- ISO-8601 UTC; campaign interval applies until then
    ('fordrojning_minuter', '30'),       -- random 0..N minutes added to every scheduled start
    ('blockerade_i_rad', '0'),           -- blocked omgångar in a row, drives backoff
    ('nasta_korning', '');               -- ISO-8601 UTC; next scheduled omgång, survives restarts
