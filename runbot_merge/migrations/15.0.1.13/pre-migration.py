def migrate(cr, version):
    cr.execute("ALTER TABLE runbot_merge_stagings "
               "ADD COLUMN staging_end timestamp without time zone")
    cr.execute("UPDATE runbot_merge_stagings SET staging_end = write_date")
