-- Version identity shared by file-backed workers until the config registry supplies versions.
create table if not exists backend_file_rule_revisions (
    rule_id text primary key,
    content_hash text not null,
    version integer not null
);
