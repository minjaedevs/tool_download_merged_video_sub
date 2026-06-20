-- ================================================================
-- phimngan.tv  - Supabase schema
-- 2 bảng:
--   1. phimngan_movies     - danh sách phim (từ list page)
--   2. phimngan_episodes   - chi tiết tập (từ detail page, lưu raw JSON)
-- ================================================================

create extension if not exists pg_trgm;
create extension if not exists unaccent;

-- ─────────────────────────────────────────────────────────────────
-- Bảng 1: phimngan_movies
-- ─────────────────────────────────────────────────────────────────
create table if not exists public.phimngan_movies (
    id bigserial primary key,

    -- Identity & tên
    slug          text not null unique,
    name          text not null,

    -- Thumbnail
    thumbnail     text,

    -- Episode metadata (từ list page – không cần gọi API detail)
    episode_count      integer,          -- số tập đã đăng (80)
    total_episode      integer,          -- tổng số tập (80)
    episode_count_text  text,             -- "80/80 tập"  (text gốc)

    -- Thống kê (từ list page)
    views       bigint,                 -- "1.3m" → 1300000
    likes       integer,
    comments    integer,

    -- Badges
    is_hot      boolean not null default false,   -- HOT
    is_featured boolean not null default false,   -- NỔI BẬT
    is_voice   boolean not null default false,   -- "Phiên bản lồng tiếng"

    -- Thể loại
    category    text,

    -- Ngày cập nhật gần nhất
    updated_date date,

    -- Tìm kiếm
    search_text       text not null default '',
    search_text_ascii text not null default '',

    -- Raw list-page HTML/JSON để debug / re-parse
    list_page_raw jsonb not null default '{}'::jsonb,

    -- Metadata sync
    source_page   integer,              -- page=N khi crawl được
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    synced_at     timestamptz not null default now(),
    created_at    timestamptz not null default now(),
    updated_at_ts timestamptz not null default now()
);

-- Indexes
create index if not exists phimngan_movies_slug_idx
    on public.phimngan_movies (slug);
create index if not exists phimngan_movies_name_trgm_idx
    on public.phimngan_movies using gin (name gin_trgm_ops);
create index if not exists phimngan_movies_search_text_trgm_idx
    on public.phimngan_movies using gin (search_text gin_trgm_ops);
create index if not exists phimngan_movies_search_text_ascii_trgm_idx
    on public.phimngan_movies using gin (search_text_ascii gin_trgm_ops);
create index if not exists phimngan_movies_views_idx
    on public.phimngan_movies (views desc);
create index if not exists phimngan_movies_episode_count_idx
    on public.phimngan_movies (episode_count desc);
create index if not exists phimngan_movies_is_featured_idx
    on public.phimngan_movies (is_featured) where is_featured = true;
create index if not exists phimngan_movies_is_hot_idx
    on public.phimngan_movies (is_hot) where is_hot = true;
create index if not exists phimngan_movies_synced_at_idx
    on public.phimngan_movies (synced_at desc);
create index if not exists phimngan_movies_last_seen_at_idx
    on public.phimngan_movies (last_seen_at desc);

-- RLS
alter table public.phimngan_movies enable row level security;
drop policy if exists "Public read phimngan movies" on public.phimngan_movies;
create policy "Public read phimngan movies"
    on public.phimngan_movies
    for select
    to anon, authenticated
    using (true);


-- ─────────────────────────────────────────────────────────────────
-- Bảng 2: phimngan_episodes
-- Lưu raw episode data từ detail page, mỗi tập 1 row.
-- ─────────────────────────────────────────────────────────────────
create table if not exists public.phimngan_episodes (
    id bigserial primary key,

    -- Liên kết với bảng movies
    movie_slug text not null,
    movie_name text not null,          -- denormalize: tránh JOIN khi đọc

    -- Episode identity
    ep_id      text not null,
    ep_order   integer not null,
    ep_title   text,

    -- URLs (từ detail page)
    video_url  text,
    sub_url_vi text,                   -- phụ đề tiếng Việt (hard-code CDN)
    cover_url  text,

    -- Thông tin
    duration_secs integer,

    -- Raw JSON nguyên gốc từ API detail (lưu để debug / re-parse)
    raw jsonb not null default '{}'::jsonb,

    -- Timestamps
    synced_at  timestamptz not null default now(),
    created_at timestamptz not null default now()
);

-- Unique: 1 movie + 1 order = 1 row
alter table public.phimngan_episodes
    add constraint phimngan_episodes_movie_order_unique
    unique (movie_slug, ep_order);

-- Indexes
create index if not exists phimngan_episodes_movie_slug_idx
    on public.phimngan_episodes (movie_slug);
create index if not exists phimngan_episodes_movie_order_idx
    on public.phimngan_episodes (movie_slug, ep_order);
create index if not exists phimngan_episodes_ep_id_idx
    on public.phimngan_episodes (ep_id);
create index if not exists phimngan_episodes_synced_at_idx
    on public.phimngan_episodes (synced_at desc);

-- RLS
alter table public.phimngan_episodes enable row level security;
drop policy if exists "Public read phimngan episodes" on public.phimngan_episodes;
create policy "Public read phimngan episodes"
    on public.phimngan_episodes
    for select
    to anon, authenticated
    using (true);
