def migrate(cr, version):
    cr.execute("""
    CREATE TABLE runbot_merge_events_sources (
        id serial primary key,
        repository varchar not null,
        secret varchar
    );
    INSERT INTO runbot_merge_events_sources (repository, secret)
    SELECT r.name, p.secret
    FROM runbot_merge_repository r
    JOIN runbot_merge_project p ON p.id = r.project_id;
    """)
