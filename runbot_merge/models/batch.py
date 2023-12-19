from __future__ import annotations

from odoo import models, fields


class Batch(models.Model):
    """ A batch is a "horizontal" grouping of *codependent* PRs: PRs with
    the same label & target but for different repositories. These are
    assumed to be part of the same "change" smeared over multiple
    repositories e.g. change an API in repo1, this breaks use of that API
    in repo2 which now needs to be updated.
    """
    _name = _description = 'runbot_merge.batch'

    target = fields.Many2one('runbot_merge.branch', required=True, index=True)
    staging_ids = fields.Many2many('runbot_merge.stagings')
    split_id = fields.Many2one('runbot_merge.split', index=True)

    prs = fields.One2many('runbot_merge.pull_requests', 'batch_id')

    active = fields.Boolean(default=True)
