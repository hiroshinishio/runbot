def move_fields(cr, *names):
    cr.execute("""
    UPDATE ir_model_data
       SET module = 'runbot_merge'
     WHERE module = 'forwardport'
       AND model = 'runbot_merge_pull_requests'
       AND name IN %s
    """, [names])

def migrate(cr, version):
    # cleanup some old crap
    cr.execute("""
    ALTER TABLE runbot_merge_project_freeze
        DROP COLUMN IF EXISTS release_label,
        DROP COLUMN IF EXISTS bump_label
    """)

    # fw constraint moved to mergebot, alongside all the fields it constrains
    cr.execute("""
    UPDATE ir_model_data
       SET module = 'runbot_merge'
     WHERE module = 'forwardport'
       AND model = 'ir.model.constraint'
       AND name = 'constraint_runbot_merge_pull_requests_fw_constraint'
    """)
    move_fields(
        cr, 'merge_date', 'refname',
        'limit_id', 'source_id', 'parent_id', 'root_id', 'forwardport_ids',
        'detach_reason', 'fw_policy')

    # view depends on pr.state, which prevents changing the state column's type
    # we can just drop the view and it'll be recreated by the db update
    cr.execute("DROP VIEW runbot_merge_freeze_labels")
    # convert a few data types
    cr.execute("""
    CREATE TYPE runbot_merge_pull_requests_priority_type
        AS ENUM ('default', 'priority', 'alone');

    CREATE TYPE runbot_merge_pull_requests_state_type
        AS ENUM ('opened', 'closed', 'validated', 'approved', 'ready', 'merged', 'error');

    CREATE TYPE runbot_merge_pull_requests_merge_method_type
        AS ENUM ('merge', 'rebase-merge', 'rebase-ff', 'squash');

    CREATE TYPE runbot_merge_pull_requests_status_type
        AS ENUM ('pending', 'failure', 'success');


    ALTER TABLE runbot_merge_pull_requests
        ALTER COLUMN priority
            TYPE runbot_merge_pull_requests_priority_type
            USING CASE WHEN priority = 0
                    THEN 'alone'
                    ELSE 'default'
                  END::runbot_merge_pull_requests_priority_type,
        ALTER COLUMN state
            TYPE runbot_merge_pull_requests_state_type
            USING state::runbot_merge_pull_requests_state_type,
        ALTER COLUMN merge_method
            TYPE runbot_merge_pull_requests_merge_method_type
            USING merge_method::runbot_merge_pull_requests_merge_method_type;
    """)

    cr.execute("""
    ALTER TABLE runbot_merge_pull_requests
     ADD COLUMN closed boolean not null default 'false',
     ADD COLUMN error boolean not null default 'false',
     ADD COLUMN skipchecks boolean not null default 'false',
     ADD COLUMN cancel_staging boolean not null default 'false',

     ADD COLUMN statuses text not null default '{}',
     ADD COLUMN statuses_full text not null default '{}',
     ADD COLUMN status runbot_merge_pull_requests_status_type not null default 'pending'
    """)
    # first pass: update all the new unconditional (or simple) fields
    cr.execute("""
    UPDATE runbot_merge_pull_requests p
        SET closed = state = 'closed',
            error = state = 'error',
            skipchecks = priority = 'alone',
            cancel_staging = priority = 'alone',
            fw_policy = CASE fw_policy WHEN 'ci' THEN 'default' ELSE fw_policy END,
            reviewed_by = CASE state
                -- old version did not reset reviewer on PR update
                WHEN 'opened' THEN NULL
                WHEN 'validated' THEN NULL
                -- if a PR predates the reviewed_by field, assign odoobot as reviewer
                WHEN 'merged' THEN coalesce(reviewed_by, 2)
                ELSE reviewed_by
            END,
            status = CASE state
                WHEN 'validated' THEN 'success'
                WHEN 'ready' THEN 'success'
                WHEN 'merged' THEN 'success'
                ELSE 'pending'
            END::runbot_merge_pull_requests_status_type
    """)

    # the rest only gets updated if we have a matching commit which is not
    # always the case
    cr.execute("""
    CREATE TEMPORARY TABLE parents ( id INTEGER not null, overrides jsonb not null );
    WITH RECURSIVE parent_chain AS (
         SELECT id, overrides::jsonb
           FROM runbot_merge_pull_requests
          WHERE parent_id IS NULL
    UNION ALL
         SELECT p.id, coalesce(pc.overrides || p.overrides::jsonb, pc.overrides, p.overrides::jsonb) as overrides
           FROM runbot_merge_pull_requests p
           JOIN parent_chain pc ON p.parent_id = pc.id
    )
    INSERT INTO parents SELECT * FROM parent_chain;
    CREATE INDEX ON parents (id);

    UPDATE runbot_merge_pull_requests p
       SET statuses = jsonb_pretty(c.statuses::jsonb)::text,
           statuses_full = jsonb_pretty(
               c.statuses::jsonb
            || coalesce((select overrides from parents where id = p.parent_id), '{}')
            || overrides::jsonb
           )::text
      FROM runbot_merge_commit c
     WHERE p.head = c.sha
    """)
