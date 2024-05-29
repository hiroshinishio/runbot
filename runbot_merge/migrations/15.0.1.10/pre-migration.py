""" Migration for the unified commands parser, fp_github fields moved from
forwardport to mergebot (one of them is removed but we might not care)
"""
def migrate(cr, version):
    cr.execute("""
    UPDATE ir_model_data
       SET module = 'runbot_merge'
     WHERE module = 'forwardport'
       AND model = 'ir.model.fields'
       AND name in ('fp_github_token', 'fp_github_name')
    """)
