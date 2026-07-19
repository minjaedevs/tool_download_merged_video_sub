create extension if not exists pg_trgm;
create extension if not exists unaccent;

create table if not exists public.xshort_movies (
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

    -- Where this row was last seen in the source catalog.
    source_api text not null default '',
    source_cursor text,
    source_offset integer,
    position_index integer,
    is_featured boolean not null default false,

    -- Raw list item from /api/home or /api/loadmore.
    raw jsonb not null default '{}'::jsonb,

    -- Raw detail response from /allepisode?shortPlayId=...
    -- Keep encrypted/raw payload as-is so parsers can be improved later
    -- without losing source data.
    detail_raw jsonb not null default '{}'::jsonb,
    detail_synced_at timestamptz,

    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    synced_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.xshort_movies
    add column if not exists detail_raw jsonb not null default '{}'::jsonb;

alter table public.xshort_movies
    add column if not exists detail_synced_at timestamptz;

create index if not exists xshort_movies_name_trgm_idx
    on public.xshort_movies using gin (name gin_trgm_ops);

create index if not exists xshort_movies_search_text_trgm_idx
    on public.xshort_movies using gin (search_text gin_trgm_ops);

create index if not exists xshort_movies_search_text_ascii_trgm_idx
    on public.xshort_movies using gin (search_text_ascii gin_trgm_ops);

create index if not exists xshort_movies_last_seen_at_idx
    on public.xshort_movies (last_seen_at desc);

create index if not exists xshort_movies_synced_at_idx
    on public.xshort_movies (synced_at desc);

create index if not exists xshort_movies_episode_count_idx
    on public.xshort_movies (episode_count desc);

create index if not exists xshort_movies_detail_synced_at_idx
    on public.xshort_movies (detail_synced_at desc);

alter table public.xshort_movies enable row level security;

drop policy if exists "Public read Xshort movies" on public.xshort_movies;
create policy "Public read Xshort movies"
    on public.xshort_movies
    for select
    to anon, authenticated
    using (true);
