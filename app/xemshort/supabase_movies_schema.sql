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

create table if not exists public.dramawave_movies (
    id bigserial primary key,
    source_id text,
    play_id text not null unique,
    name text not null,
    thumbnail text,
    intro text,
    label_list text,
    episode_count integer,
    search_text text not null default '',
    search_text_ascii text not null default '',
    source_api text,
    source_cursor text,
    source_offset integer,
    position_index integer,
    is_featured boolean not null default false,
    raw jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    synced_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists dramawave_movies_name_trgm_idx
    on public.dramawave_movies using gin (name gin_trgm_ops);

create index if not exists dramawave_movies_search_text_trgm_idx
    on public.dramawave_movies using gin (search_text gin_trgm_ops);

create index if not exists dramawave_movies_search_text_ascii_trgm_idx
    on public.dramawave_movies using gin (search_text_ascii gin_trgm_ops);

create index if not exists dramawave_movies_synced_at_idx
    on public.dramawave_movies (synced_at desc);

create index if not exists dramawave_movies_last_seen_at_idx
    on public.dramawave_movies (last_seen_at desc);

create index if not exists dramawave_movies_episode_count_idx
    on public.dramawave_movies (episode_count desc);

alter table public.dramawave_movies enable row level security;

drop policy if exists "Public read DramaWave movies" on public.dramawave_movies;
create policy "Public read DramaWave movies"
    on public.dramawave_movies
    for select
    to anon, authenticated
    using (true);
