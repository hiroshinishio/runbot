from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
from collections import defaultdict
from collections.abc import Iterator

import requests
from psycopg2 import sql

from odoo import models, fields, api
from .utils import enum
from .. import git

_logger = logging.getLogger(__name__)
FOOTER = '\nMore info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port\n'


class StagingBatch(models.Model):
    _name = 'runbot_merge.staging.batch'
    _description = "link between batches and staging in order to maintain an " \
                   "ordering relationship between the batches of a staging"
    _log_access = False
    _order = 'id'

    runbot_merge_batch_id = fields.Many2one('runbot_merge.batch', required=True)
    runbot_merge_stagings_id = fields.Many2one('runbot_merge.stagings', required=True)

    def init(self):
        super().init()

        self.env.cr.execute(sql.SQL("""
        CREATE UNIQUE INDEX IF NOT EXISTS runbot_merge_staging_batch_idx
            ON {table} (runbot_merge_stagings_id, runbot_merge_batch_id);

        CREATE INDEX IF NOT EXISTS runbot_merge_staging_batch_rev
            ON {table} (runbot_merge_batch_id) INCLUDE (runbot_merge_stagings_id);
        """).format(table=sql.Identifier(self._table)))


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
    _parent_store = True
    _order = "id desc"

    name = fields.Char(compute="_compute_name", search="_search_name")
    target = fields.Many2one('runbot_merge.branch', store=True, compute='_compute_target')
    batch_staging_ids = fields.One2many('runbot_merge.staging.batch', 'runbot_merge_batch_id')
    staging_ids = fields.Many2many(
        'runbot_merge.stagings',
        compute="_compute_staging_ids",
        context={'active_test': False},
    )
    split_id = fields.Many2one('runbot_merge.split', index=True)

    all_prs = fields.One2many('runbot_merge.pull_requests', 'batch_id')
    prs = fields.One2many('runbot_merge.pull_requests', compute='_compute_open_prs', search='_search_open_prs')
    active = fields.Boolean(compute='_compute_active', store=True, help="closed batches (batches containing only closed PRs)")

    fw_policy = fields.Selection([
        ('no', "Do not port forward"),
        ('default', "Default"),
        ('skipci', "Skip CI"),
    ], required=True, default="default", string="Forward Port Policy", tracking=True)

    merge_date = fields.Datetime(tracking=True)
    # having skipchecks skip both validation *and approval* makes sense because
    # it's batch-wise, having to approve individual PRs is annoying
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
    priority = fields.Selection([
        ('default', "Default"),
        ('priority', "Priority"),
        ('alone', "Alone"),
    ], default='default', group_operator=None, required=True, tracking=True,
        column_type=enum(_name, 'priority'),
    )

    blocked = fields.Char(store=True, compute="_compute_blocked")

    # unlike on PRs, this does not get detached... ? (because batches can be
    # partially detached so that's a PR-level concern)
    parent_path = fields.Char(index=True)
    parent_id = fields.Many2one("runbot_merge.batch")
    genealogy_ids = fields.Many2many(
        "runbot_merge.batch",
        compute="_compute_genealogy",
        context={"active_test": False},
    )

    @api.depends('batch_staging_ids.runbot_merge_stagings_id')
    def _compute_staging_ids(self):
        for batch in self:
            batch.staging_ids = batch.batch_staging_ids.runbot_merge_stagings_id

    @property
    def source(self):
        return self.browse(map(int, self.parent_path.split('/', 1)[:1]))

    def descendants(self, include_self: bool = False) -> Iterator[Batch]:
        # in DB both will prefix-match on the literal prefix then apply a
        # trivial filter (even though the filter is technically unnecessary for
        # the first form), doing it like this means we don't have to `- self`
        # in the ``not include_self`` case
        if include_self:
            pattern = self.parent_path + '%'
        else:
            pattern = self.parent_path + '_%'

        act = self.env.context.get('active_test', True)
        return self\
            .with_context(active_test=False)\
            .search([("parent_path", '=like', pattern)], order="parent_path")\
            .with_context(active_test=act)

    # also depends on all the descendants of the source or sth
    @api.depends('parent_path')
    def _compute_genealogy(self):
        for batch in self:
            sid = next(iter(batch.parent_path.split('/', 1)))
            batch.genealogy_ids = self \
                .with_context(active_test=False)\
                .search([("parent_path", "=like", f"{sid}/%")], order="parent_path")\

    def _auto_init(self):
        for field in self._fields.values():
            if not isinstance(field, fields.Selection) or field.column_type[0] == 'varchar':
                continue

            t = field.column_type[1]
            self.env.cr.execute("SELECT FROM pg_type WHERE typname = %s", [t])
            if not self.env.cr.rowcount:
                self.env.cr.execute(
                    f"CREATE TYPE {t} AS ENUM %s",
                    [tuple(s for s, _ in field.selection)]
                )

        super()._auto_init()

        self.env.cr.execute("""
        CREATE INDEX IF NOT EXISTS runbot_merge_batch_ready_idx
        ON runbot_merge_batch (target, priority)
        WHERE blocked IS NULL;

        CREATE INDEX IF NOT EXISTS runbot_merge_batch_parent_id_idx
        ON runbot_merge_batch (parent_id)
        WHERE parent_id IS NOT NULL;
        """)

    @api.depends('all_prs.closed')
    def _compute_active(self):
        for b in self:
            b.active = not all(p.closed for p in b.all_prs)

    @api.depends('all_prs.closed')
    def _compute_open_prs(self):
        for b in self:
            b.prs = b.all_prs.filtered(lambda p: not p.closed)

    def _search_open_prs(self, operator, value):
        return [('all_prs', operator, value), ('active', '=', True)]

    @api.depends("prs.label")
    def _compute_name(self):
        for batch in self:
            batch.name = batch.prs[:1].label or batch.all_prs[:1].label

    def _search_name(self, operator, value):
        return [('all_prs.label', operator, value)]

    @api.depends("all_prs.target", "all_prs.closed")
    def _compute_target(self):
        for batch in self:
            targets = batch.prs.mapped('target') or batch.all_prs.mapped('target')
            batch.target = targets if len(targets) == 1 else False

    @api.depends(
        "merge_date",
        "prs.error", "prs.draft", "prs.squash", "prs.merge_method",
        "skipchecks",
        "prs.status", "prs.reviewed_by", "prs.target",
    )
    def _compute_blocked(self):
        for batch in self:
            if batch.merge_date:
                batch.blocked = "Merged."
            elif not batch.active:
                batch.blocked = "all prs are closed"
            elif len(targets := batch.prs.mapped('target')) > 1:
                batch.blocked = f"Multiple target branches: {', '.join(targets.mapped('name'))!r}"
            elif blocking := batch.prs.filtered(
                lambda p: p.error or p.draft or not (p.squash or p.merge_method)
            ):
                batch.blocked = "Pull request(s) %s blocked." % ', '.join(blocking.mapped('display_name'))
            elif not batch.skipchecks and (unready := batch.prs.filtered(
                lambda p: not (p.reviewed_by and p.status == "success")
            )):
                unreviewed = ', '.join(unready.filtered(lambda p: not p.reviewed_by).mapped('display_name'))
                unvalidated = ', '.join(unready.filtered(lambda p: p.status == 'pending').mapped('display_name'))
                failed = ', '.join(unready.filtered(lambda p: p.status == 'failure').mapped('display_name'))
                batch.blocked = "Pull request(s) %s." % ', '.join(filter(None, [
                    unreviewed and f"{unreviewed} are waiting for review",
                    unvalidated and f"{unvalidated} are waiting for CI",
                    failed and f"{failed} have failed CI",
                ]))
            else:
                if batch.blocked and batch.cancel_staging:
                    if splits := batch.target.split_ids:
                        splits.unlink()
                    batch.target.active_staging_id.cancel(
                        'unstaged by %s becoming ready',
                        ', '.join(batch.prs.mapped('display_name')),
                    )
                batch.blocked = False


    def _port_forward(self):
        if not self:
            return

        proj = self.target.project_id
        if not proj.fp_github_token:
            _logger.warning(
                "Can not forward-port %s (%s): no token on project %s",
                self,
                ', '.join(self.prs.mapped('display_name')),
                proj.name
            )
            return

        notarget = [r.name for r in self.prs.repository if not r.fp_remote_target]
        if notarget:
            _logger.error(
                "Can not forward-port %s (%s): repos %s don't have a forward port remote configured",
                self,
                ', '.join(self.prs.mapped('display_name')),
                ', '.join(notarget),
            )
            return

        all_sources = [(p.source_id or p) for p in self.prs]
        all_targets = [p._find_next_target() for p in self.prs]

        if all(t is None for t in all_targets):
            # TODO: maybe add a feedback message?
            _logger.info(
                "Will not forward port %s (%s): no next target",
                self,
                ', '.join(self.prs.mapped('display_name'))
            )
            return

        PRs = self.env['runbot_merge.pull_requests']
        targets = defaultdict(lambda: PRs)
        for p, t in zip(self.prs, all_targets):
            if t:
                targets[t] |= p
            else:
                _logger.info("Skip forward porting %s (of %s): no next target", p.display_name, self)


        # all the PRs *with a next target* should have the same, we can have PRs
        # stopping forward port earlier but skipping... probably not
        if len(targets) != 1:
            for t, prs in targets.items():
                linked, other = next((
                    (linked, other)
                    for other, linkeds in targets.items()
                    if other != t
                    for linked in linkeds
                ))
                for pr in prs:
                    self.env.ref('runbot_merge.forwardport.failure.discrepancy')._send(
                        repository=pr.repository,
                        pull_request=pr.number,
                        token_field='fp_github_token',
                        format_args={'pr': pr, 'linked': linked, 'next': t.name, 'other': other.name},
                    )
            _logger.warning(
                "Cancelling forward-port of %s (%s): found different next branches (%s)",
                self,
                ', '.join(self.prs.mapped('display_name')),
                ', '.join(t.name for t in targets),
            )
            return

        target, prs = next(iter(targets.items()))
        # this is run by the cron, no need to check if otherwise scheduled:
        # either the scheduled job is this one, or it's an other scheduling
        # which will run after this one and will see the port already exists
        if self.search_count([('parent_id', '=', self.id), ('target', '=', target.id)]):
            _logger.warning(
                "Will not forward-port %s (%s): already ported",
                self,
                ', '.join(prs.mapped('display_name'))
            )
            return

        # the base PR is the PR with the "oldest" target
        base = max(all_sources, key=lambda p: (p.target.sequence, p.target.name))
        # take only the branch bit
        new_branch = '%s-%s-%s-fw' % (
            target.name,
            base.refname,
            # avoid collisions between fp branches (labels can be reused
            # or conflict especially as we're chopping off the owner)
            base64.urlsafe_b64encode(os.urandom(3)).decode()
        )
        conflicts = {}
        for pr in prs:
            repo = git.get_local(pr.repository)
            conflicts[pr], head = pr._create_fp_branch(repo, target)

            repo.push(git.fw_url(pr.repository), f"{head}:refs/heads/{new_branch}")

        gh = requests.Session()
        gh.headers['Authorization'] = 'token %s' % proj.fp_github_token
        has_conflicts = any(conflicts.values())
        # could create a batch here but then we'd have to update `_from_gh` to
        # take a batch and then `create` to not automatically resolve batches,
        # easier to not do that.
        new_batch = PRs.browse(())
        self.env.cr.execute('LOCK runbot_merge_pull_requests IN SHARE MODE')
        for pr in prs:
            owner, _ = pr.repository.fp_remote_target.split('/', 1)
            source = pr.source_id or pr
            root = pr.root_id

            message = source.message + '\n\n' + '\n'.join(
                "Forward-Port-Of: %s" % p.display_name
                for p in root | source
            )

            title, body = re.match(r'(?P<title>[^\n]+)\n*(?P<body>.*)', message, flags=re.DOTALL).groups()
            r = gh.post(f'https://api.github.com/repos/{pr.repository.name}/pulls', json={
                'base': target.name,
                'head': f'{owner}:{new_branch}',
                'title': '[FW]' + (' ' if title[0] != '[' else '') + title,
                'body': body
            })
            if not r.ok:
                _logger.warning("Failed to create forward-port PR for %s, deleting branches", pr.display_name)
                # delete all the branches this should automatically close the
                # PRs if we've created any. Using the API here is probably
                # simpler than going through the working copies
                for repo in prs.mapped('repository'):
                    d = gh.delete(f'https://api.github.com/repos/{repo.fp_remote_target}/git/refs/heads/{new_branch}')
                    if d.ok:
                        _logger.info("Deleting %s:%s=success", repo.fp_remote_target, new_branch)
                    else:
                        _logger.warning("Deleting %s:%s=%s", repo.fp_remote_target, new_branch, d.text)
                raise RuntimeError(f"Forwardport failure: {pr.display_name} ({r.text})")

            new_pr = PRs._from_gh(r.json())
            _logger.info("Created forward-port PR %s", new_pr)
            new_batch |= new_pr

            # allows PR author to close or skipci
            new_pr.write({
                'merge_method': pr.merge_method,
                'source_id': source.id,
                # only link to previous PR of sequence if cherrypick passed
                'parent_id': pr.id if not has_conflicts else False,
                'detach_reason': "conflicts:\n{}".format('\n\n'.join(
                    f"{out}\n{err}".strip()
                    for _, out, err, _ in filter(None, conflicts.values())
                )) if has_conflicts else None,
            })
            if has_conflicts and pr.parent_id and pr.state not in ('merged', 'closed'):
                self.env.ref('runbot_merge.forwardport.failure.conflict')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'source': source, 'pr': pr, 'new': new_pr, 'footer': FOOTER},
                )

        for pr, new_pr in zip(prs, new_batch):
            new_pr._fp_conflict_feedback(pr, conflicts)

            labels = ['forwardport']
            if has_conflicts:
                labels.append('conflict')
            self.env['runbot_merge.pull_requests.tagging'].create({
                'repository': new_pr.repository.id,
                'pull_request': new_pr.number,
                'tags_add': labels,
            })

        new_batch = new_batch.batch_id
        new_batch.parent_id = self
        # try to schedule followup
        new_batch._schedule_fp_followup()
        return new_batch

    def _schedule_fp_followup(self, *, force_fw=False):
        _logger = logging.getLogger(__name__).getChild('forwardport.next')
        # if the PR has a parent and is CI-validated, enqueue the next PR
        scheduled = self.browse(())
        for batch in self:
            prs = ', '.join(batch.prs.mapped('display_name'))
            _logger.info('Checking if forward-port %s (%s)', batch, prs)
            # in cas of conflict or update individual PRs will "lose" their
            # parent, which should prevent forward porting
            #
            # even if we force_fw, a *followup* should still only be for forward
            # ports so check that the batch has a parent (which should be the
            # same thing as all the PRs having a source, kinda, but cheaper,
            # it's not entirely true as technically the user could have added a
            # PR to the forward ported batch
            if not (batch.parent_id and force_fw or all(p.parent_id for p in batch.prs)):
                _logger.info('-> no parent %s (%s)', batch, prs)
                continue
            if not force_fw and batch.source.fw_policy != 'skipci' \
                    and (invalid := batch.prs.filtered(lambda p: p.state not in ['validated', 'ready'])):
                _logger.info(
                    '-> wrong state %s (%s)',
                    batch,
                    ', '.join(f"{p.display_name}: {p.state}" for p in invalid),
                )
                continue

            # check if we've already forward-ported this branch
            next_target = batch._find_next_targets()
            if not next_target:
                _logger.info("-> forward port done (no next target)")
                continue
            if len(next_target) > 1:
                _logger.error(
                    "-> cancelling forward-port of %s (%s): inconsistent next target branch (%s)",
                    batch,
                    prs,
                    ', '.join(next_target.mapped('name')),
                )

            if n := self.search([
                ('target', '=', next_target.id),
                ('parent_id', '=', batch.id),
            ], limit=1):
                _logger.info('-> already forward-ported (%s)', n)
                continue

            _logger.info("check pending port for %s (%s)", batch, prs)
            if self.env['forwardport.batches'].search_count([('batch_id', '=', batch.id)]):
                _logger.warning('-> already recorded')
                continue

            _logger.info('-> ok')
            self.env['forwardport.batches'].create({
                'batch_id': batch.id,
                'source': 'fp',
            })
            scheduled |= batch
        return scheduled

    def _find_next_target(self):
        """Retrieves the next target from every PR, and returns it if it's the
        same for all the PRs which have one (PRs without a next target are
        ignored, this is considered acceptable).

        If the next targets are inconsistent, returns no next target.
        """
        next_target = self._find_next_targets()
        if len(next_target) == 1:
            return next_target
        else:
            return self.env['runbot_merge.branch'].browse(())

    def _find_next_targets(self):
        return self.prs.mapped(lambda p: p._find_next_target() or self.env['runbot_merge.branch'])

    def write(self, vals):
        if vals.get('merge_date'):
            # TODO: remove condition when everything is merged
            remover = self.env.get('forwardport.branch_remover')
            if remover is not None:
                remover.create([
                    {'pr_id': p.id}
                    for b in self
                    if not b.merge_date
                    for p in b.prs
                ])

        if vals.get('fw_policy') == 'skipci':
            nonskip = self.filtered(lambda b: b.fw_policy != 'skipci')
        else:
            nonskip = self.browse(())
        super().write(vals)

        # if we change the policy to skip CI, schedule followups on merged
        # batches which were not previously marked as skipping CI
        if nonskip:
            toggled = nonskip.filtered(lambda b: b.merge_date)
            tips = toggled.mapped(lambda b: b.genealogy_ids[-1:])
            for tip in tips:
                tip._schedule_fp_followup()

        return True

    @api.ondelete(at_uninstall=True)
    def _on_delete_clear_stagings(self):
        self.batch_staging_ids.unlink()

    def unlink(self):
        """
        batches can be unlinked if they:

        - have run out of PRs
        - and don't have a parent batch (which is not being deleted)
        - and don't have a child batch (which is not being deleted)

        this is to keep track of forward port histories at the batch level
        """
        unlinkable = self.filtered(
            lambda b: not (b.prs or (b.parent_id - self) or (self.search([('parent_id', '=', b.id)]) - self))
        )
        return super(Batch, unlinkable).unlink()
