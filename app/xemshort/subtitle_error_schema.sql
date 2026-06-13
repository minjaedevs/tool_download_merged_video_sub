create extension if not exists pg_trgm;
create extension if not exists unaccent;

-- One shared table for subtitle problems from all movie sources.
-- Each movie has one row per source; broken episodes are stored in error_episodes.
create table if not exists public.subtitle_error_movies (
    id bigserial primary key,
    source text not null,
    play_id text not null,
    movie_name text not null,
    error_episodes jsonb not null default '[]'::jsonb,
    error_episode_count integer not null default 0,
    error_episode_numbers integer[] not null default '{}'::integer[],
    error_episode_names text not null default '',
    last_error_episode_id text,
    last_error_episode_number integer,
    last_error_episode_name text,
    last_error_note text,
    last_error_at timestamptz not null default now(),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint subtitle_error_movies_source_play_id_key unique (source, play_id),
    constraint subtitle_error_movies_source_not_blank check (length(trim(source)) > 0),
    constraint subtitle_error_movies_play_id_not_blank check (length(trim(play_id)) > 0)
);

alter table public.subtitle_error_movies
    add column if not exists error_episode_numbers integer[] not null default '{}'::integer[];

alter table public.subtitle_error_movies
    add column if not exists error_episode_names text not null default '';

create index if not exists subtitle_error_movies_source_idx
    on public.subtitle_error_movies (source);

create index if not exists subtitle_error_movies_last_error_at_idx
    on public.subtitle_error_movies (last_error_at desc);

create index if not exists subtitle_error_movies_error_episode_numbers_idx
    on public.subtitle_error_movies using gin (error_episode_numbers);

create index if not exists subtitle_error_movies_movie_name_trgm_idx
    on public.subtitle_error_movies using gin (movie_name gin_trgm_ops);

create index if not exists subtitle_error_movies_error_episodes_gin_idx
    on public.subtitle_error_movies using gin (error_episodes);

alter table public.subtitle_error_movies enable row level security;

drop policy if exists "Public read subtitle error movies" on public.subtitle_error_movies;
create policy "Public read subtitle error movies"
    on public.subtitle_error_movies
    for select
    to anon, authenticated
    using (true);

-- Use this RPC/upsert from each source-specific worker.
-- It keeps one row per movie/source and replaces the note for the same episode.
create or replace function public.upsert_subtitle_error_movie(
    p_source text,
    p_play_id text,
    p_movie_name text,
    p_episode_id text,
    p_episode_number integer,
    p_episode_name text,
    p_error_note text,
    p_raw jsonb default '{}'::jsonb
)
returns public.subtitle_error_movies
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now timestamptz := now();
    v_episode jsonb;
    v_row public.subtitle_error_movies;
begin
    v_episode := jsonb_build_object(
        'episode_id', p_episode_id,
        'episode_number', p_episode_number,
        'episode_name', p_episode_name,
        'error_note', p_error_note,
        'last_error_at', v_now
    );

    insert into public.subtitle_error_movies (
        source,
        play_id,
        movie_name,
        error_episodes,
        error_episode_count,
        last_error_episode_id,
        last_error_episode_number,
        last_error_episode_name,
        last_error_note,
        last_error_at,
        raw,
        created_at,
        updated_at
    )
    values (
        p_source,
        p_play_id,
        p_movie_name,
        jsonb_build_array(v_episode),
        1,
        p_episode_id,
        p_episode_number,
        p_episode_name,
        p_error_note,
        v_now,
        coalesce(p_raw, '{}'::jsonb),
        v_now,
        v_now
    )
    on conflict (source, play_id) do update
    set
        movie_name = excluded.movie_name,
        error_episodes = (
            select coalesce(jsonb_agg(item), '[]'::jsonb) || jsonb_build_array(v_episode)
            from jsonb_array_elements(public.subtitle_error_movies.error_episodes) as item
            where not (
                (coalesce(p_episode_id, '') <> '' and item->>'episode_id' = p_episode_id)
                or (p_episode_number is not null and nullif(item->>'episode_number', '')::integer = p_episode_number)
                or (coalesce(p_episode_name, '') <> '' and item->>'episode_name' = p_episode_name)
            )
        ),
        last_error_episode_id = excluded.last_error_episode_id,
        last_error_episode_number = excluded.last_error_episode_number,
        last_error_episode_name = excluded.last_error_episode_name,
        last_error_note = excluded.last_error_note,
        last_error_at = excluded.last_error_at,
        raw = coalesce(excluded.raw, public.subtitle_error_movies.raw),
        updated_at = v_now
    returning * into v_row;

    update public.subtitle_error_movies
    set
        error_episode_count = jsonb_array_length(error_episodes),
        error_episode_numbers = coalesce((
            select array_agg(ep_num order by ep_num)
            from (
                select distinct nullif(item->>'episode_number', '')::integer as ep_num
                from jsonb_array_elements(error_episodes) as item
                where nullif(item->>'episode_number', '') is not null
            ) as nums
            where ep_num is not null
        ), '{}'::integer[]),
        error_episode_names = coalesce((
            select string_agg(ep_name, ', ' order by ep_num nulls last, ep_name)
            from (
                select distinct
                    nullif(item->>'episode_number', '')::integer as ep_num,
                    coalesce(nullif(item->>'episode_name', ''), 'Tap ' || nullif(item->>'episode_number', '')) as ep_name
                from jsonb_array_elements(error_episodes) as item
            ) as names
            where ep_name is not null
        ), '')
    where id = v_row.id
    returning * into v_row;

    return v_row;
end;
$$;

revoke all on function public.upsert_subtitle_error_movie(
    text, text, text, text, integer, text, text, jsonb
) from public;

grant execute on function public.upsert_subtitle_error_movie(
    text, text, text, text, integer, text, text, jsonb
) to service_role;
