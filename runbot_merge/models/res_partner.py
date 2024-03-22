import random
from email.utils import parseaddr

from markupsafe import Markup, escape

import odoo.tools
from odoo import fields, models, tools, api, Command

from .. import github

class CIText(fields.Char):
    type = 'char'
    column_type = ('citext', 'citext')
    column_cast_from = ('varchar', 'text')

class Partner(models.Model):
    _name = 'res.partner'
    _inherit = ['res.partner', 'mail.thread']

    email = fields.Char(index=True)
    github_login = CIText()
    delegate_reviewer = fields.Many2many('runbot_merge.pull_requests')
    formatted_email = fields.Char(string="commit email", compute='_rfc5322_formatted')
    review_rights = fields.One2many('res.partner.review', 'partner_id')
    override_rights = fields.Many2many('res.partner.override')
    override_sensitive = fields.Boolean(compute="_compute_sensitive_overrides")

    def _auto_init(self):
        res = super(Partner, self)._auto_init()
        tools.create_unique_index(
            self._cr, 'runbot_merge_unique_gh_login', self._table, ['github_login'])
        return res

    @api.depends('name', 'email', 'github_login')
    def _rfc5322_formatted(self):
        for partner in self:
            if partner.email:
                email = parseaddr(partner.email)[1]
            elif partner.github_login:
                email = '%s@users.noreply.github.com' % partner.github_login
            else:
                email = ''
            partner.formatted_email = '%s <%s>' % (partner.name, email)

    def fetch_github_email(self):
        # this requires a token in order to fetch the email field, otherwise
        # it's just not returned, select a random project to fetch
        gh = github.GH(random.choice(self.env['runbot_merge.project'].search([])).github_token, None)
        for p in self.filtered(lambda p: p.github_login and p.email is False):
            p.email = gh.user(p.github_login)['email'] or False
        return False

    @api.depends("override_rights.context")
    def _compute_sensitive_overrides(self):
        for p in self:
            p.override_sensitive = any(o.context == 'ci/security' for o in p.override_rights)

    def write(self, vals):
        created = []
        updated = {}
        deleted = set()
        for cmd, id, values in vals.get('review_rights', []):
            if cmd == Command.DELETE:
                deleted.add(id)
            elif cmd == Command.CREATE:
                # 'repository_id': 3, 'review': True, 'self_review': False
                created.append(values)
            elif cmd == Command.UPDATE:
                updated[id] = values
            #  could also be LINK for records which are not touched but we don't care

        new_rights = None
        if r := vals.get('override_rights'):
            # only handle reset (for now?) even though technically e.g. 0 works
            # the web client doesn't seem to use it (?)
            if r[0][0] == 6:
                new_rights = self.env['res.partner.override'].browse(r[0][2])

        Repo = self.env['runbot_merge.repository'].browse
        for p in self:
            msgs = []
            if ds := p.review_rights.filtered(lambda r: r.id in deleted):
                msgs.append("removed review rights on {}\n".format(
                    ', '.join(ds.mapped('repository_id.name'))
                ))
            if us := p.review_rights.filtered(lambda r: r.id in updated):
                msgs.extend(
                    "updated review rights on {}: {}\n".format(
                        u.repository_id.name,
                        ', '.join(
                            f'allowed {f}' if v else f'forbid {f}'
                            for f in ['review', 'self_review']
                            if (v := updated[u.id].get(f)) is not None
                        )
                    )
                    for u in us
                )
            msgs.extend(
                'added review rights on {}: {}\n'.format(
                    Repo(c['repository_id']).name,
                    ', '.join(filter(c.get, ['review', 'self_review'])),
                )
                for c in created
            )
            if new_rights is not None:
                for r in p.override_rights - new_rights:
                    msgs.append(f"removed override rights for {r.context!r} on {r.repository_id.name}")
                for r in new_rights - p.override_rights:
                    msgs.append(f"added override rights for {r.context!r} on {r.repository_id.name}")
            if msgs:
                p._message_log(body=Markup('<ul>{}</ul>').format(Markup().join(
                    map(Markup('<li>{}</li>').format, reversed(msgs))
                )))

        return super().write(vals)


class PartnerMerge(models.TransientModel):
    _inherit = 'base.partner.merge.automatic.wizard'

    @api.model
    def _update_values(self, src_partners, dst_partner):
        # sift down through src partners, removing all github_login and keeping
        # the last one
        new_login = None
        for p in src_partners:
            new_login = p.github_login or new_login
        if new_login:
            src_partners.write({'github_login': False})
        if new_login and not dst_partner.github_login:
            dst_partner.github_login = new_login
        super()._update_values(src_partners, dst_partner)

class ReviewRights(models.Model):
    _name = 'res.partner.review'
    _description = "mapping of review rights between partners and repos"

    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade')
    repository_id = fields.Many2one('runbot_merge.repository', required=True)
    review = fields.Boolean(default=False)
    self_review = fields.Boolean(default=False)

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_unique_index(self._cr, 'runbot_merge_review_m2m', self._table, ['partner_id', 'repository_id'])
        return res

    def name_get(self):
        return [
            (r.id, '%s: %s' % (r.repository_id.name, ', '.join(filter(None, [
                r.review and "reviewer",
                r.self_review and "self-reviewer"
            ]))))
            for r in self
        ]

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [('repository_id.name', operator, name)], limit=limit).name_get()

class OverrideRights(models.Model):
    _name = 'res.partner.override'
    _description = 'lints which the partner can override'

    partner_ids = fields.Many2many('res.partner')
    repository_id = fields.Many2one('runbot_merge.repository')
    context = fields.Char(required=True)

    def init(self):
        super().init()
        tools.create_unique_index(
            self.env.cr, 'res_partner_override_unique', self._table,
            ['context', 'coalesce(repository_id, 0)']
        )

    @api.model_create_multi
    def create(self, vals_list):
        for partner, contexts in odoo.tools.groupby((
            (partner_id, vals['context'], vals['repository_id'])
            for vals in vals_list
            # partner_ids is of the form [Command.set(ids)
            for partner_id in vals.get('partner_ids', [(None, None, [])])[0][2]
        ), lambda p: p[0]):
            partner = self.env['res.partner'].browse(partner)
            for _, context, repository in contexts:
                repository = self.env['runbot_merge.repository'].browse(repository)
                partner._message_log(body=f"added override rights for {context!r} on {repository.name}")

        return super().create(vals_list)

    def write(self, vals):
        new = None
        if pids := vals.get('partner_ids'):
            new = self.env['res.partner'].browse(pids[0][2])
        if new is not None:
            for o in self:
                added = new - o.partner_ids
                removed = o.partner_ids - new
                for p in added:
                    p._message_log(body=f"added override rights for {o.context!r} on {o.repository_id.name}")
                for r in removed:
                    r._message_log(body=f"removed override rights for {o.context!r} on {o.repository_id.name}")

        return super().write(vals)

    def unlink(self):
        for o in self:
            for p in o.partner_ids:
                p._message_log(body=f"removed override rights for {o.context!r} on {o.repository_id.name}")
        return super().unlink()

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return self.search((args or []) + [
            '|', ('context', operator, name),
                 ('repository_id.name', operator, name)
        ], limit=limit).name_get()

    def name_get(self):
        return [
            (r.id, f'{r.repository_id.name}: {r.context}' if r.repository_id else r.context)
            for r in self
        ]
