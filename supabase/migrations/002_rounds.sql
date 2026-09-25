-- Only needed if you ran an earlier schema.sql before rounds were added.
alter table events      add column if not exists current_round int not null default 1;
alter table wines       add column if not exists round_no int not null default 1 check (round_no >= 1);
alter table wines       drop constraint if exists wines_pour_no_check;
alter table wines       add constraint wines_pour_no_check check (pour_no between 1 and 99);
alter table event_state add column if not exists current_round int not null default 1;
alter table event_state add column if not exists round_count int not null default 1;
