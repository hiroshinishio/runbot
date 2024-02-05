from __future__ import annotations

from odoo import models, fields, api


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

    fw_policy = fields.Selection([
        ('default', "Default"),
        ('skipci', "Skip CI"),
    ], required=True, default="default", string="Forward Port Policy")

    merge_date = fields.Datetime(tracking=True)
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

    blocked = fields.Char(store=True, compute="_compute_stageable")

    @api.depends(
        "merge_date",
        "prs.error", "prs.draft", "prs.squash", "prs.merge_method",
        "skipchecks", "prs.status", "prs.reviewed_by",
    )
    def _compute_stageable(self):
        for batch in self:
            if batch.merge_date:
                batch.blocked = "Merged."
            elif blocking := batch.prs.filtered(
                lambda p: p.error or p.draft or not (p.squash or p.merge_method)
            ):
                batch.blocked = "Pull request(s) %s blocked." % (
                    p.display_name for p in blocking
                )
            elif not batch.skipchecks and (unready := batch.prs.filtered(
                lambda p: not (p.reviewed_by and p.status == "success")
            )):
                batch.blocked = "Pull request(s) %s waiting for CI." % ', '.join(
                    p.display_name for p in unready
                )
            else:
                batch.blocked = False
