-- Trial workspaces now start empty. Remove the old scripted demo rows from
-- existing workspaces so no customer sees fabricated comments as real data.
DELETE FROM scheduled_deletion sd
USING comment_content c
WHERE sd.comment_id = c.comment_id AND c.page_id = 'demo-page';

DELETE FROM action a
USING comment_content c
WHERE a.comment_id = c.comment_id AND c.page_id = 'demo-page';

DELETE FROM verdict v
USING comment_content c
WHERE v.comment_id = c.comment_id AND c.page_id = 'demo-page';

DELETE FROM correction k
USING comment_content c
WHERE k.comment_id = c.comment_id AND c.page_id = 'demo-page';

DELETE FROM comment_content
WHERE page_id = 'demo-page';

UPDATE workspace
SET is_sandbox = FALSE
WHERE is_sandbox = TRUE;
