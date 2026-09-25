-- Wine tasting app schema. Run once in the Supabase SQL editor.
-- Paper entries may record only a total, so the category columns allow nulls.
-- The FastAPI server connects with the Postgres connection string (full access).
-- Browsers only get the anon key, and RLS limits them to reading event_state.

create extension if not exists pgcrypto;

create table members (
  id          uuid primary key default gen_random_uuid(),
  name        text not null unique,
  created_at  timestamptz not null default now()
);

create table events (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,               -- "October 2026 tasting"
  theme       text,                        -- "Oregon Pinot Noir"
  held_on     date not null,
  location    text,
  join_code   text not null unique,        -- short code guests type or scan
  status      text not null default 'setup'
              check (status in ('setup','open','locked','revealing','done')),
  current_round int not null default 1,     -- the round being scored or revealed
  created_at  timestamptz not null default now()
);

create table wines (
  id          uuid primary key default gen_random_uuid(),
  event_id    uuid not null references events(id) on delete cascade,
  round_no    int  not null default 1 check (round_no >= 1),    -- taste a round, then reveal it
  pour_no     int  not null check (pour_no between 1 and 99),  -- the blind number
  name        text not null,               -- hidden until reveal
  producer    text,
  appellation text,
  vintage     int,
  price       numeric(8,2),
  abv         numeric(4,2),
  soil        text,
  notes       text,
  twin_of     uuid references wines(id),   -- set on the secret duplicate pour
  unique (event_id, pour_no)
);

create table scores (
  id          uuid primary key default gen_random_uuid(),
  event_id    uuid not null references events(id) on delete cascade,
  wine_id     uuid not null references wines(id) on delete cascade,
  member_id   uuid not null references members(id),
  appearance  numeric(3,1) check (appearance between 0 and 3),
  aroma       numeric(3,1) check (aroma      between 0 and 6),
  taste       numeric(3,1) check (taste      between 0 and 6),
  aftertaste  numeric(3,1) check (aftertaste between 0 and 3),
  overall     numeric(3,1) check (overall    between 0 and 2),
  total       numeric(4,1) not null check (total between 0 and 20),
  price_guess numeric(8,2),
  note        text,
  entered_by  text not null default 'self' check (entered_by in ('self','host','import')),
  updated_at  timestamptz not null default now(),
  unique (wine_id, member_id)
);
create index scores_event_idx on scores(event_id);

-- The only table browsers can read. It never holds wine identities.
create table event_state (
  event_id      uuid primary key references events(id) on delete cascade,
  status        text not null,
  current_round int  not null default 1,
  round_count   int  not null default 1,
  reveal_step   int  not null default 0,
  wine_count    int  not null default 0,    -- wines in the current round
  tasters       int  not null default 0,    -- members with a score in the current round
  finished      int  not null default 0,    -- members who scored every wine in the round
  updated_at    timestamptz not null default now()
);

alter table members     enable row level security;
alter table events      enable row level security;
alter table wines       enable row level security;
alter table scores      enable row level security;
alter table event_state enable row level security;

create policy "anyone can watch event state"
  on event_state for select to anon, authenticated using (true);

alter publication supabase_realtime add table event_state;
