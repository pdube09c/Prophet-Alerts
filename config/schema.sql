-- Design B §7 — hosted DB schema (Supabase / Postgres), sport-aware from the
-- start so multi-sport logging + the combined morning summary work later.
--
-- Apply once in the Supabase SQL editor. The workflows connect with the
-- service_role key (SUPABASEKEY) and bypass RLS. The static selection page
-- connects with the anon key and is restricted by the RLS policies at the
-- bottom: it may READ survivors and INSERT bets, nothing else.

create table if not exists snapshots (
  id          bigint generated always as identity primary key,
  sport       text        not null,
  game_date   date        not null,
  game_id     text        not null,
  taken_at    timestamptz not null,
  book        text        not null,
  home_point  real,
  home_ml     integer,
  away_ml     integer,
  home_limit  real,
  away_limit  real,
  unique (sport, game_id, taken_at, book)
);
create index if not exists snapshots_game_idx on snapshots (sport, game_date, game_id, taken_at);

create table if not exists stats (
  sport      text  not null,
  asof_date  date  not null,
  team       text  not null,
  "group"    text  not null,
  field      text  not null,
  value      real  not null,
  primary key (sport, asof_date, team, "group", field)
);

create table if not exists survivors (
  id         bigint generated always as identity primary key,
  sport      text        not null,
  game_date  date        not null,
  game_id    text        not null,
  favorite   text        not null,
  dog        text        not null,
  entry_ml   integer     not null,
  liquidity  real,
  tip_time   timestamptz not null,
  alerted    boolean     not null default false,
  created_at timestamptz not null default now(),
  unique (sport, game_id)
);

create table if not exists bets (
  id                text         primary key,
  sport             text         not null,
  game_date         date         not null,
  favorite          text         not null,
  dog               text         not null,
  entry_ml          integer      not null,
  liquidity         real,
  stake_chosen      real         not null,
  entry_time_actual timestamptz,
  placed            boolean      not null default false,
  result            text,                 -- null | 'win' | 'loss'
  net_pnl           real,
  created_at        timestamptz  not null default now()
);

create table if not exists vetoed (
  id            bigint generated always as identity primary key,
  sport         text    not null,
  game_date     date    not null,
  favorite      text    not null,
  dog           text    not null,
  ml            integer,
  reason        text    not null,         -- '+'-joined layer names, e.g. 'book+grinder'
  favwin_actual boolean
);

-- Row-Level Security: the anon key (static page) reads survivors + writes bets.
alter table survivors enable row level security;
alter table bets      enable row level security;
alter table snapshots enable row level security;
alter table stats     enable row level security;
alter table vetoed    enable row level security;

drop policy if exists anon_read_survivors on survivors;
create policy anon_read_survivors on survivors for select to anon using (true);

drop policy if exists anon_insert_bets on bets;
create policy anon_insert_bets on bets for insert to anon with check (true);

drop policy if exists anon_read_bets on bets;
create policy anon_read_bets on bets for select to anon using (true);
-- snapshots/stats/vetoed have RLS on and NO anon policy => anon has no access.
-- The service_role key used by the workflows bypasses RLS entirely.
