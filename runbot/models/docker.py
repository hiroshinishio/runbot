import logging
import re
from odoo import models, fields, api
from odoo.addons.base.models.ir_qweb import QWebException

_logger = logging.getLogger(__name__)


class Dockerfile(models.Model):
    _name = 'runbot.dockerfile'
    _inherit = [ 'mail.thread' ]
    _description = "Dockerfile"

    name = fields.Char('Dockerfile name', required=True, help="Name of Dockerfile")
    image_tag = fields.Char(compute='_compute_image_tag', store=True)
    template_id = fields.Many2one('ir.ui.view', string='Docker Template', domain=[('type', '=', 'qweb')], context={'default_type': 'qweb', 'default_arch_base': '<t></t>'})
    arch_base = fields.Text(related='template_id.arch_base', readonly=False, related_sudo=True)
    dockerfile = fields.Text(compute='_compute_dockerfile', tracking=True)
    to_build = fields.Boolean('To Build', help='Build Dockerfile. Check this when the Dockerfile is ready.', default=False)
    version_ids = fields.One2many('runbot.version', 'dockerfile_id', string='Versions')
    description = fields.Text('Description')
    view_ids = fields.Many2many('ir.ui.view', compute='_compute_view_ids', groups="runbot.group_runbot_admin")
    project_ids = fields.One2many('runbot.project', 'dockerfile_id', string='Default for Projects')
    bundle_ids = fields.One2many('runbot.bundle', 'dockerfile_id', string='Used in Bundles')
    build_results = fields.One2many('runbot.docker_build_result', 'dockerfile_id', string='Build results')
    last_successful_result = fields.Many2one('runbot.docker_build_result', compute='_compute_last_successful_result')

    _sql_constraints = [('runbot_dockerfile_name_unique', 'unique(name)', 'A Dockerfile with this name already exists')]

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        copied_record = super().copy(default={'name': '%s (copy)' % self.name, 'to_build': False})
        copied_record.template_id = self.template_id.copy()
        copied_record.template_id.name = '%s (copy)' % copied_record.template_id.name
        copied_record.template_id.key = '%s (copy)' % copied_record.template_id.key
        return copied_record

    def _compute_last_successful_result(self):
        for record in self:
            record.last_successful_result = next((result for result in record.build_results if result.result == 'success'), record.build_results.browse())

    @api.depends('template_id.arch_base')
    def _compute_dockerfile(self):
        for rec in self:
            try:
                res = rec.template_id._render_template(rec.template_id.id) if rec.template_id else ''
                rec.dockerfile = re.sub(r'^\s*$', '', res, flags=re.M).strip()
            except QWebException:
                rec.dockerfile = ''

    @api.depends('name')
    def _compute_image_tag(self):
        for rec in self:
            if rec.name:
                rec.image_tag = 'odoo:%s' % re.sub(r'[ /:\(\)\[\]]', '', rec.name)

    @api.depends('template_id')
    def _compute_view_ids(self):
        for rec in self:
            keys = re.findall(r'<t.+t-call="(.+)".+', rec.arch_base or '')
            rec.view_ids = self.env['ir.ui.view'].search([('type', '=', 'qweb'), ('key', 'in', keys)]).ids


class DockerBuildOutput(models.Model):
    _name = 'runbot.docker_build_result'
    _description = "Result of a docker file build"
    _order = 'id desc'

    result = fields.Selection(string="Result", selection=[('error', 'Error'), ('success', 'Success')])
    host_id = fields.Many2one('runbot.host', string="Host")
    duration = fields.Float("Exec time")
    dockerfile_id = fields.Many2one('runbot.dockerfile', string="Docker file")
    output = fields.Text('Output')
    content = fields.Text('Content')
    identifier = fields.Char('Identifier')
    summary = fields.Char("Summary", compute='_compute_summary', store=True)

    @api.depends('output')
    def _compute_summary(self):
        for record in self:
            summary = ''
            for line in reversed(self.output.split('\n')):
                if len(line) > 5:
                    summary = line
                    break
            record.summary = summary
