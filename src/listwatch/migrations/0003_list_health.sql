-- Ops notifications per list (spec "Drift": lista ej åtkomlig, parserfel > 20 %).
ALTER TABLE wishlist ADD COLUMN fel_i_rad INTEGER NOT NULL DEFAULT 0;  -- failed reads in a row, not counting blocks
ALTER TABLE wishlist ADD COLUMN parserfel INTEGER NOT NULL DEFAULT 0 CHECK (parserfel IN (0, 1));  -- notified, until back under
