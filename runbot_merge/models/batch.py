from __future__ import annotations

from odoo import models, fields


class Batch(models.Model):
    """ A batch is a "horizontal" grouping of *codependent* PRs: PRs with
    the same label & target but for different repositories. These are
    assumed to be part of the same "change" smeared over multiple
    repositories e.g. change an API in repo1, this breaks use of that API
    in repo2 which now needs to be updated.
    """
    _name = 'runbot_merge.batch'
    _description = "batch of pull request"
    _inherit = ['mail.thread']

    target = fields.Many2one('runbot_merge.branch', required=True, index=True)
    staging_ids = fields.Many2many('runbot_merge.stagings')
    split_id = fields.Many2one('runbot_merge.split', index=True)

    prs = fields.One2many('runbot_merge.pull_requests', 'batch_id')

    active = fields.Boolean(default=True)

    skipchecks = fields.Boolean(
        string="Skips Checks",
        default=False, tracking=True,
        help="Forces entire batch to be ready, skips validation and approval",
    )
    cancel_staging = fields.Boolean(
        string="Cancels Stagings",
        default=False, tracking=True,
        help="Cancels current staging on target branch when becoming ready"
    )
