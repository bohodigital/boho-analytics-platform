-- Schema version 4. Semantic cutover for forms evidence identity version 3.
--
-- The prior identity can contain historical zero facts collected outside D1
-- retention or before the mailbox observation horizon. Facts remain immutable
-- lineage; current readers select identity version 3. Advancing schema_meta via
-- the migration runner makes schema-v3 application code refuse this database
-- instead of silently reactivating identity-version-2 facts during rollback.
SELECT 1;
