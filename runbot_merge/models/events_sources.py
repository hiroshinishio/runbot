from odoo import models, fields


class EventsSources(models.Model):
    _name = 'runbot_merge.events_sources'
    _description = 'Valid Webhook Event Sources'
    _order = "repository"
    _rec_name = "repository"

    # FIXME: unique repo? Or allow multiple secrets per repo?
    repository = fields.Char(index=True, required=True)
    secret = fields.Char()
