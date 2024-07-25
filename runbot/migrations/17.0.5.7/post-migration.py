import logging

from markupsafe import Markup

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    dockerfiles = env['runbot.dockerfile'].search([])
    for dockerfile in dockerfiles:
        if dockerfile.template_id and not dockerfile.layer_ids:
            dockerfile._template_to_layers()

    for dockerfile in dockerfiles:
        if dockerfile.template_id and dockerfile.layer_ids:
            dockerfile.message_post(
                body=Markup('Was using template <a href="http://127.0.0.1:8069/web#id=%s&model=ir.ui.view&view_type=form">%s</a>') % (dockerfile.template_id.id, dockerfile.template_id.name)
            )
            dockerfile.template_id = False
