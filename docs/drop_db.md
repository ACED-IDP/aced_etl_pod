
psql -U postgres -W
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'sheepdog_local';
DROP DATABASE sheepdog_local;
