"""Completely missed that in 44084e303ccece3cb54128ab29eab399bd4d24e9 I
completely changed the semantics and structure of the statuses_cache, so the
old caches don't actually work anymore at all.

This rewrites all existing caches.
"""
def migrate(cr, version):
    cr.execute("""
WITH statuses AS (
    SELECT
        s.id as staging_id,
        json_object_agg(c.sha, c.statuses::json) as statuses
    FROM runbot_merge_stagings s
    LEFT JOIN runbot_merge_stagings_heads h ON (h.staging_id = s.id)
    LEFT JOIN runbot_merge_commit c ON (h.commit_id = c.id)
    GROUP BY s.id
)
UPDATE runbot_merge_stagings
SET statuses_cache = statuses
FROM statuses
WHERE id = staging_id
    """)
