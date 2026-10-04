-- Who asked for a rollback. Nullable: automatic (drift-triggered) rollbacks have no requester and every
-- existing row keeps working unchanged.
ALTER TABLE rollbacks ADD COLUMN requested_by VARCHAR(255);
