-- queue_crawl_schema.sql
-- Run this in Supabase SQL Editor to create the crawl queue tables.
-- Created for XemShort Crawl Queue system.

-- ── Bảng yêu cầu crawl ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.queue_request_movie_aller (
    id            BIGSERIAL PRIMARY KEY,
    "shortPlayId" TEXT NOT NULL UNIQUE,
    name          TEXT,
    author        TEXT[] NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'processing', 'completed', 'error')),
    notes         TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);

-- Index để fetch_oldest_pending nhanh
CREATE INDEX IF NOT EXISTS idx_queue_request_status
    ON public.queue_request_movie_aller(status, created_at);

COMMENT ON TABLE public.queue_request_movie_aller IS
    'Hàng đợi yêu cầu crawl video URL từ XemShort';
COMMENT ON COLUMN public.queue_request_movie_aller."shortPlayId" IS
    'ID phim trên XemShort';
COMMENT ON COLUMN public.queue_request_movie_aller.name IS
    'Tên phim — được điền bởi crawl_service khi lần đầu xử lý (showName từ run_capture.py)';
COMMENT ON COLUMN public.queue_request_movie_aller.author IS
    'Danh sách người yêu cầu (array, append nếu trùng ID)';
COMMENT ON COLUMN public.queue_request_movie_aller.status IS
    'pending → processing → completed | error';

-- ── Bảng kết quả crawl ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public."nestShort_crawl" (
    id               BIGSERIAL PRIMARY KEY,
    "shortPlayId"    TEXT NOT NULL UNIQUE,
    "showName"       TEXT,
    total            INT DEFAULT 0,
    captured         INT DEFAULT 0,
    missing_episodes JSONB DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'crawling', 'completed', 'failed')),
    raw              JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    completed_at     TIMESTAMPTZ
);

COMMENT ON TABLE public."nestShort_crawl" IS
    'Kết quả crawl URL video từ XemShort (dùng Frida)';
COMMENT ON COLUMN public."nestShort_crawl".raw IS
    'Full JSON output từ run_capture.py (episodes array với signed URL)';
COMMENT ON COLUMN public."nestShort_crawl".status IS
    'pending → crawling → completed | failed';

-- ── Row Level Security ────────────────────────────────────────────────────

-- queue_request_movie_aller: public read, service_role write (bypass RLS automatically)
ALTER TABLE public.queue_request_movie_aller ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "queue_public_select" ON public.queue_request_movie_aller;
CREATE POLICY "queue_public_select" ON public.queue_request_movie_aller
    FOR SELECT USING (true);

-- nestShort_crawl: public read
ALTER TABLE public."nestShort_crawl" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "crawl_public_select" ON public."nestShort_crawl";
CREATE POLICY "crawl_public_select" ON public."nestShort_crawl"
    FOR SELECT USING (true);

-- Note: Writes use service_role key which bypasses RLS automatically.
-- No need to create write policies.
