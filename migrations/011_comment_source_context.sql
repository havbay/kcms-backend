ALTER TABLE comment_content
    ADD COLUMN IF NOT EXISTS post_kind TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (post_kind IN ('TEXT', 'IMAGE', 'VIDEO', 'UNKNOWN'));

ALTER TABLE comment_content
    ADD COLUMN IF NOT EXISTS post_permalink TEXT;

-- Existing rows belong only to authored sandbox datasets at this point. Give
-- them the same source context new sandbox workspaces receive.
UPDATE comment_content c
SET post_text = COALESCE(
        c.post_text,
        'វីដេអូថ្មី៖ សូមចែករំលែកមតិយោបល់របស់អ្នកអំពីសេវាកម្មរបស់យើង។'
    ),
    post_kind = CASE WHEN c.post_kind = 'UNKNOWN' THEN 'VIDEO' ELSE c.post_kind END
FROM workspace w
WHERE c.workspace_id = w.id AND w.is_sandbox = TRUE;

UPDATE comment_content
SET parent_text = 'តើអ្នកគិតយ៉ាងម៉េចអំពីមតិនេះ?', is_reply = TRUE
WHERE comment_id LIKE '%-c-004' AND workspace_id IS NOT NULL;
