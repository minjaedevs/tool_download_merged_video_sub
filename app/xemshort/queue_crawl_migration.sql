-- queue_crawl_migration.sql
-- Chạy file này nếu đã tạo bảng từ queue_crawl_schema.sql trước đó.

-- Migration 1: thêm cột completed_at
ALTER TABLE public.queue_request_movie_aller
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

ALTER TABLE public."nestShort_crawl"
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

COMMENT ON COLUMN public.queue_request_movie_aller.completed_at IS
    'Thời điểm crawl hoàn thành (được set bởi crawl_service khi status=completed)';

COMMENT ON COLUMN public."nestShort_crawl".completed_at IS
    'Thời điểm crawl hoàn thành';

-- Migration 2: thêm cột name (tên phim)
ALTER TABLE public.queue_request_movie_aller
    ADD COLUMN IF NOT EXISTS name TEXT;

COMMENT ON COLUMN public.queue_request_movie_aller.name IS
    'Tên phim — được điền bởi crawl_service khi lần đầu xử lý (showName từ run_capture.py)';
