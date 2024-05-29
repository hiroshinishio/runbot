def migrate(cr, version):
    cr.execute("ALTER TABLE runbot_merge_project DROP COLUMN IF EXISTS fp_github_email")
    cr.execute("""
    ALTER TABLE runbot_merge_branch
        DROP COLUMN IF EXISTS fp_sequence,
        DROP COLUMN IF EXISTS fp_target
    """)
