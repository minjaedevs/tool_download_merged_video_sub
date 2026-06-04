create extension if not exists pg_trgm;
create extension if not exists unaccent;

create table if not exists public.netshort_movies (
    id bigserial primary key,
    source_id text,
    play_id text not null unique,
    name text not null,
    thumbnail text,
    intro text,
    label_list text,
    search_text text not null default '',
    search_text_ascii text not null default '',
    source_page integer,
    source_api text,
    raw jsonb not null default '{}'::jsonb,
    synced_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.netshort_movies
    add column if not exists search_text_ascii text not null default '';

update public.netshort_movies
set search_text_ascii = lower(unaccent(search_text))
where search_text_ascii = ''
  and search_text <> '';

create index if not exists xemshort_movies_name_trgm_idx
    on public.netshort_movies using gin (name gin_trgm_ops);

create index if not exists xemshort_movies_search_text_trgm_idx
    on public.netshort_movies using gin (search_text gin_trgm_ops);

create index if not exists xemshort_movies_search_text_ascii_trgm_idx
    on public.netshort_movies using gin (search_text_ascii gin_trgm_ops);

create index if not exists xemshort_movies_synced_at_idx
    on public.netshort_movies (synced_at desc);

alter table public.netshort_movies enable row level security;

drop policy if exists "Public read NetShort movies" on public.netshort_movies;
create policy "Public read NetShort movies"
    on public.netshort_movies
    for select
    to anon, authenticated
    using (true);
