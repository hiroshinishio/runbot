# -*- coding: utf-8 -*-

import logging


def migrate(cr, version):
    cr.execute('ALTER TABLE runbot_commit_link ADD COLUMN repo_id INT')
    cr.execute('ALTER TABLE runbot_commit_link ADD COLUMN sequence INT')
    cr.execute('''
               UPDATE runbot_commit_link
               SET repo_id=commit.repo_id, sequence = repo.sequence
               FROM runbot_commit commit
               JOIN runbot_repo repo ON commit.repo_id=repo.id
               WHERE commit.id = runbot_commit_link.commit_id
               ''')
