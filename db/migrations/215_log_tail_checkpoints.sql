create table if not exists log_tail_checkpoints (
  source_id   text    not null,
  path        text    not null,
  file_id     text,
  byte_offset bigint  not null default 0 check (byte_offset >= 0),
  anchor      bytea   not null default '\x',
  discarding  boolean not null default false,
  primary key (source_id, path)
);
