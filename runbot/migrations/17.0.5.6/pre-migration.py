# -*- coding: utf-8 -*-

import logging


def migrate(cr, version):
    cr.execute('ALTER TABLE runbot_build ADD COLUMN create_batch_id INT')
