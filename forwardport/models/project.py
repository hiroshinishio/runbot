# -*- coding: utf-8 -*-
"""
Technically could be independent from mergebot but would require a lot of
duplicate work e.g. keeping track of statuses (including on commits which
might not be in PRs yet), handling co-dependent PRs, ...

However extending the mergebot also leads to messiness: fpbot should have
its own user / feedback / API keys, mergebot and fpbot both have branch
ordering but for mergebot it's completely cosmetics, being slaved to mergebot
means PR creation is trickier (as mergebot assumes opened event will always
lead to PR creation but fpbot wants to attach meaning to the PR when setting
it up), ...
"""
from __future__ import annotations

import datetime
import itertools
import json
import logging
import operator
import subprocess
import tempfile
import typing
from pathlib import Path

import dateutil.relativedelta
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError
from odoo.osv import expression
from odoo.tools.misc import topological_sort, groupby
from odoo.tools.appdirs import user_cache_dir
from odoo.addons.base.models.res_partner import Partner
from odoo.addons.runbot_merge import git, utils
from odoo.addons.runbot_merge.models.pull_requests import Branch
from odoo.addons.runbot_merge.models.stagings_create import Message

DEFAULT_DELTA = dateutil.relativedelta.relativedelta(days=3)

_logger = logging.getLogger('odoo.addons.forwardport')

class Project(models.Model):
    _inherit = 'runbot_merge.project'

    id: int
    github_prefix: str

    def write(self, vals):
        # check on branches both active and inactive so disabling branches doesn't
        # make it look like the sequence changed.
        self_ = self.with_context(active_test=False)
        previously_active_branches = {project: project.branch_ids.filtered('active') for project in self_}
        branches_before = {project: project._forward_port_ordered() for project in self_}

        r = super().write(vals)
        self_._followup_prs(previously_active_branches)
        self_._insert_intermediate_prs(branches_before)
        return r

    def _followup_prs(self, previously_active_branches):
        """If a branch has been disabled and had PRs without a followup (e.g.
        because no CI or CI failed), create followup, as if the branch had been
        originally disabled (and thus skipped over)
        """
        Batch = self.env['runbot_merge.batch']
        ported = self.env['runbot_merge.pull_requests']
        for p in self:
            actives = previously_active_branches[p]
            for deactivated in p.branch_ids.filtered(lambda b: not b.active) & actives:
                # if a non-merged batch targets a deactivated branch which is
                # not its limit
                extant = Batch.search([
                    ('parent_id', '!=', False),
                    ('target', '=', deactivated.id),
                    # if at least one of the PRs has a different limit
                    ('prs.limit_id', '!=', deactivated.id),
                    ('merge_date', '=', False),
                ]).filtered(lambda b:\
                    # and has a next target (should already be a function of
                    # the search but doesn't hurt)
                    b._find_next_target() \
                    # and has not already been forward ported
                    and Batch.search_count([('parent_id', '=', b.id)]) == 0
                )
                ported |= extant.prs.filtered(lambda p: p._find_next_target())
                # enqueue a forward port as if the now deactivated branch had
                # been skipped over (which is the normal fw behaviour)
                for b in extant.with_context(force_fw=True):
                    # otherwise enqueue a followup
                    b._schedule_fp_followup()

        if not ported:
            return

        for feedback in self.env['runbot_merge.pull_requests.feedback'].search(expression.OR(
            [('repository', '=', p.repository.id), ('pull_request', '=', p.number)]
            for p in ported
        )):
            # FIXME: better signal
            if 'disabled' in feedback.message:
                feedback.message += '\n\nAs this was not its limit, it will automatically be forward ported to the next active branch.'

    def _insert_intermediate_prs(self, branches_before):
        """If new branches have been added to the sequence inbetween existing
        branches (mostly a freeze inserted before the main branch), fill in
        forward-ports for existing sequences
        """
        Branches = self.env['runbot_merge.branch']
        for p in self:
            # check if the branches sequence has been modified
            bbefore = branches_before[p]
            bafter = p._forward_port_ordered()
            if bafter.ids == bbefore.ids:
                continue

            logger = _logger.getChild('project').getChild(p.name)
            logger.debug("branches updated %s -> %s", bbefore, bafter)
            # if it's just that a branch was inserted at the end forwardport
            # should keep on keeping normally
            if bafter.ids[:-1] == bbefore.ids:
                continue

            if bafter <= bbefore:
                raise UserError("Branches can not be reordered or removed after saving.")

            # Last possibility: branch was inserted but not at end, get all
            # branches before and all branches after
            before = new = after = Branches
            for b in bafter:
                if b in bbefore:
                    if new:
                        after += b
                    else:
                        before += b
                else:
                    if new:
                        raise UserError("Inserting multiple branches at the same time is not supported")
                    new = b
            logger.debug('before: %s new: %s after: %s', before.ids, new.ids, after.ids)
            # find all FPs whose ancestry spans the insertion
            leaves = self.env['runbot_merge.pull_requests'].search([
                ('state', 'not in', ['closed', 'merged']),
                ('target', 'in', after.ids),
                ('source_id.target', 'in', before.ids),
            ])
            # get all PRs just preceding the insertion point which either are
            # sources of the above or have the same source
            candidates = self.env['runbot_merge.pull_requests'].search([
                ('target', '=', before[-1].id),
                '|', ('id', 'in', leaves.mapped('source_id').ids),
                     ('source_id', 'in', leaves.mapped('source_id').ids),
            ])
            logger.debug("\nPRs spanning new: %s\nto port: %s", leaves, candidates)
            # enqueue the creation of a new forward-port based on our candidates
            # but it should only create a single step and needs to stitch back
            # the parents linked list, so it has a special type
            for _, cs in groupby(candidates, key=lambda p: p.label):
                self.env['forwardport.batches'].create({
                    'batch_id': cs[0].batch_id.id,
                    'source': 'insert',
                })

class Repository(models.Model):
    _inherit = 'runbot_merge.repository'

    id: int
    project_id: Project
    name: str
    branch_filter: str
    fp_remote_target = fields.Char(help="where FP branches get pushed")

class PullRequests(models.Model):
    _inherit = 'runbot_merge.pull_requests'

    id: int
    display_name: str
    number: int
    repository: Repository
    target: Branch
    reviewed_by: Partner
    head: str
    state: str
    merge_date: datetime.datetime
    parent_id: PullRequests

    reminder_backoff_factor = fields.Integer(default=-4, group_operator=None)

    @api.model_create_single
    def create(self, vals):
        # PR opened event always creates a new PR, override so we can precreate PRs
        existing = self.search([
            ('repository', '=', vals['repository']),
            ('number', '=', vals['number']),
        ])
        if existing:
            return existing

        if vals.get('parent_id') and 'source_id' not in vals:
            vals['source_id'] = self.browse(vals['parent_id']).root_id.id
        pr = super().create(vals)

        # added a new PR to an already forward-ported batch: port the PR
        if self.env['runbot_merge.batch'].search_count([
            ('parent_id', '=', pr.batch_id.id),
        ]):
            self.env['forwardport.batches'].create({
                'batch_id': pr.batch_id.id,
                'source': 'complete',
                'pr_id': pr.id,
            })

        return pr

    def write(self, vals):
        # if the PR's head is updated, detach (should split off the FP lines as this is not the original code)
        # TODO: better way to do this? Especially because we don't want to
        #       recursively create updates
        # also a bit odd to only handle updating 1 head at a time, but then
        # again 2 PRs with same head is weird so...
        newhead = vals.get('head')
        with_parents = {
            p: p.parent_id
            for p in self
            if p.state not in ('merged', 'closed')
            if p.parent_id
        }
        closed_fp = self.filtered(lambda p: p.state == 'closed' and p.source_id)
        if newhead and not self.env.context.get('ignore_head_update') and newhead != self.head:
            vals.setdefault('parent_id', False)
            if with_parents and vals['parent_id'] is False:
                vals['detach_reason'] = f"Head updated from {self.head} to {newhead}"
            # if any children, this is an FP PR being updated, enqueue
            # updating children
            if self.search_count([('parent_id', '=', self.id)]):
                self.env['forwardport.updates'].create({
                    'original_root': self.root_id.id,
                    'new_root': self.id
                })

        if vals.get('parent_id') and 'source_id' not in vals:
            parent = self.browse(vals['parent_id'])
            vals['source_id'] = (parent.source_id or parent).id
        r = super().write(vals)
        if self.env.context.get('forwardport_detach_warn', True):
            for p, parent in with_parents.items():
                if p.parent_id:
                    continue
                self.env.ref('runbot_merge.forwardport.update.detached')._send(
                    repository=p.repository,
                    pull_request=p.number,
                    token_field='fp_github_token',
                    format_args={'pr': p},
                )
                if parent.state not in ('closed', 'merged'):
                    self.env.ref('runbot_merge.forwardport.update.parent')._send(
                        repository=parent.repository,
                        pull_request=parent.number,
                        token_field='fp_github_token',
                        format_args={'pr': parent, 'child': p},
                    )
        return r

    def _commits_lazy(self):
        s = requests.Session()
        s.headers['Authorization'] = 'token %s' % self.repository.project_id.fp_github_token
        for page in itertools.count(1):
            r = s.get('https://api.github.com/repos/{}/pulls/{}/commits'.format(
                self.repository.name,
                self.number
            ), params={'page': page})
            r.raise_for_status()
            yield from r.json()
            if not r.links.get('next'):
                return

    def commits(self):
        """ Returns a PR's commits oldest first (that's what GH does &
        is what we want)
        """
        commits = list(self._commits_lazy())
        # map shas to the position the commit *should* have
        idx =  {
            c: i
            for i, c in enumerate(topological_sort({
                c['sha']: [p['sha'] for p in c['parents']]
                for c in commits
            }))
        }
        return sorted(commits, key=lambda c: idx[c['sha']])


    def _create_fp_branch(self, target_branch, fp_branch_name, cleanup):
        """ Creates a forward-port for the current PR to ``target_branch`` under
        ``fp_branch_name``.

        :param target_branch: the branch to port forward to
        :param fp_branch_name: the name of the branch to create the FP under
        :param ExitStack cleanup: so the working directories can be cleaned up
        :return: A pair of an optional conflict information and a repository. If
                 present the conflict information is composed of the hash of the
                 conflicting commit, the stderr and stdout of the failed
                 cherrypick and a list of all PR commit hashes
        :rtype: (None | (str, str, str, list[commit]), Repo)
        """
        logger = _logger.getChild(str(self.id))
        root = self.root_id
        logger.info(
            "Forward-porting %s (%s) to %s",
            self.display_name, root.display_name, target_branch.name
        )
        source = git.get_local(self.repository, 'fp_github')
        r = source.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT).fetch()
        logger.info("Updated cache repo %s:\n%s", source._directory, r.stdout.decode())

        logger.info("Create working copy...")
        cache_dir = user_cache_dir('forwardport')
        # PullRequest.display_name is `owner/repo#number`, so `owner` becomes a
        # directory, `TemporaryDirectory` only creates the leaf, so we need to
        # make sure `owner` exists in `cache_dir`.
        Path(cache_dir, root.repository.name).parent.mkdir(parents=True, exist_ok=True)
        working_copy = source.clone(
            cleanup.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=f'{root.display_name}-to-{target_branch.name}',
                    dir=cache_dir)),
            branch=target_branch.name
        )

        r = working_copy.with_config(stdout=subprocess.PIPE, stderr=subprocess.STDOUT) \
            .fetch(git.source_url(self.repository, 'fp_github'), root.head)
        logger.info(
            "Fetched head of %s into %s:\n%s",
            root.display_name,
            working_copy._directory,
            r.stdout.decode()
        )
        if working_copy.check(False).cat_file(e=root.head).returncode:
            raise ForwardPortError(
                f"During forward port of {self.display_name}, unable to find "
                f"expected head of {root.display_name} ({root.head})"
            )

        project_id = self.repository.project_id
        # add target remote
        working_copy.remote(
            'add', 'target',
            'https://{p.fp_github_token}@github.com/{r.fp_remote_target}'.format(
                r=self.repository,
                p=project_id
            )
        )
        logger.info("Create FP branch %s in %s", fp_branch_name, working_copy._directory)
        working_copy.checkout(b=fp_branch_name)

        try:
            root._cherry_pick(working_copy)
            return None, working_copy
        except CherrypickError as e:
            h, out, err, commits = e.args

            # using git diff | git apply -3 to get the entire conflict set
            # turns out to not work correctly: in case files have been moved
            # / removed (which turns out to be a common source of conflicts
            # when forward-porting) it'll just do nothing to the working copy
            # so the "conflict commit" will be empty
            # switch to a squashed-pr branch
            working_copy.check(True).checkout('-bsquashed', root.head)
            # commits returns oldest first, so youngest (head) last
            head_commit = commits[-1]['commit']

            to_tuple = operator.itemgetter('name', 'email')
            def to_dict(term, vals):
                return {'GIT_%s_NAME' % term: vals[0], 'GIT_%s_EMAIL' % term: vals[1], 'GIT_%s_DATE' % term: vals[2]}
            authors, committers = set(), set()
            for c in (c['commit'] for c in commits):
                authors.add(to_tuple(c['author']))
                committers.add(to_tuple(c['committer']))
            fp_authorship = (project_id.fp_github_name, '', '')
            author = fp_authorship if len(authors) != 1 \
                else authors.pop() + (head_commit['author']['date'],)
            committer = fp_authorship if len(committers) != 1 \
                else committers.pop() + (head_commit['committer']['date'],)
            conf = working_copy.with_config(env={
                **to_dict('AUTHOR', author),
                **to_dict('COMMITTER', committer),
                'GIT_COMMITTER_DATE': '',
            })
            # squash to a single commit
            conf.reset('--soft', commits[0]['parents'][0]['sha'])
            conf.commit(a=True, message="temp")
            squashed = conf.stdout().rev_parse('HEAD').stdout.strip().decode()

            # switch back to the PR branch
            conf.checkout(fp_branch_name)
            # cherry-pick the squashed commit to generate the conflict
            conf.with_params(
                'merge.renamelimit=0',
                'merge.renames=copies',
                'merge.conflictstyle=zdiff3'
            ) \
                .with_config(check=False) \
                .cherry_pick(squashed, no_commit=True, strategy="ort")
            status = conf.stdout().status(short=True, untracked_files='no').stdout.decode()
            if err.strip():
                err = err.rstrip() + '\n----------\nstatus:\n' + status
            else:
                err = 'status:\n' + status

            # if there was a single commit, reuse its message when committing
            # the conflict
            if len(commits) == 1:
                msg = root._make_fp_message(commits[0])
            else:
                out = utils.shorten(out, 8*1024, '[...]')
                err = utils.shorten(err, 8*1024, '[...]')
                msg = f"""Cherry pick of {h} failed

stdout:
{out}
stderr:
{err}
"""

            conf.with_config(input=str(msg).encode()) \
                .commit(all=True, allow_empty=True, file='-')

            return (h, out, err, [c['sha'] for c in commits]), working_copy

    def _cherry_pick(self, working_copy):
        """ Cherrypicks ``self`` into the working copy

        :return: ``True`` if the cherrypick was successful, ``False`` otherwise
        """
        # <xxx>.cherrypick.<number>
        logger = _logger.getChild(str(self.id)).getChild('cherrypick')

        # original head so we can reset
        original_head = working_copy.stdout().rev_parse('HEAD').stdout.decode().strip()

        commits = self.commits()
        logger.info("%s: copy %s commits to %s\n%s", self, len(commits), original_head, '\n'.join(
            '- %s (%s)' % (c['sha'], c['commit']['message'].splitlines()[0])
            for c in commits
        ))

        conf_base = working_copy.with_params(
            'merge.renamelimit=0',
            'merge.renames=copies',
        ).with_config(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False
        )
        for commit in commits:
            commit_sha = commit['sha']
            # config (global -c) or commit options don't really give access to
            # setting dates
            cm = commit['commit'] # get the "git" commit object rather than the "github" commit resource
            env = {
                'GIT_AUTHOR_NAME': cm['author']['name'],
                'GIT_AUTHOR_EMAIL': cm['author']['email'],
                'GIT_AUTHOR_DATE': cm['author']['date'],
                'GIT_COMMITTER_NAME': cm['committer']['name'],
                'GIT_COMMITTER_EMAIL': cm['committer']['email'],
            }
            configured = working_copy.with_config(env=env)

            conf = conf_base.with_config(env={**env, 'GIT_TRACE': 'true'})
            # first try with default / low renamelimit
            r = conf.cherry_pick(commit_sha, strategy="ort")
            logger.debug("Cherry-picked %s: %s\n%s\n%s", commit_sha, r.returncode, r.stdout.decode(), _clean_rename(r.stderr.decode()))

            if r.returncode: # pick failed, reset and bail
                # try to log inflateInit: out of memory errors as warning, they
                # seem to return the status code 128
                logger.log(
                    logging.WARNING if r.returncode == 128 else logging.INFO,
                    "forward-port of %s (%s) failed at %s",
                    self, self.display_name, commit_sha)
                configured.reset('--hard', original_head)
                raise CherrypickError(
                    commit_sha,
                    r.stdout.decode(),
                    _clean_rename(r.stderr.decode()),
                    commits
                )

            msg = self._make_fp_message(commit)

            # replace existing commit message with massaged one
            configured \
                .with_config(input=str(msg).encode())\
                .commit(amend=True, file='-')
            result = configured.stdout().rev_parse('HEAD').stdout.decode()
            logger.info('%s: success -> %s', commit_sha, result)

    def _make_fp_message(self, commit):
        cmap = json.loads(self.commits_map)
        msg = Message.from_message(commit['commit']['message'])
        # write the *merged* commit as "original", not the PR's
        msg.headers['x-original-commit'] = cmap.get(commit['sha'], commit['sha'])
        # don't stringify so caller can still perform alterations
        return msg

    def _outstanding(self, cutoff: str) -> typing.ItemsView[PullRequests, list[PullRequests]]:
        """ Returns "outstanding" (unmerged and unclosed) forward-ports whose
        source was merged before ``cutoff`` (all of them if not provided).

        :param cutoff: a datetime (ISO-8601 formatted)
        :returns: an iterator of (source, forward_ports)
        """
        return groupby(self.env['runbot_merge.pull_requests'].search([
            # only FP PRs
            ('source_id', '!=', False),
            # active
            ('state', 'not in', ['merged', 'closed']),
            ('source_id.merge_date', '<', cutoff),
        ], order='source_id, id'), lambda p: p.source_id)

    def _reminder(self):
        cutoff = self.env.context.get('forwardport_updated_before') \
              or fields.Datetime.to_string(datetime.datetime.now() - DEFAULT_DELTA)
        cutoff_dt = fields.Datetime.from_string(cutoff)

        for source, prs in self._outstanding(cutoff):
            backoff = dateutil.relativedelta.relativedelta(days=2**source.reminder_backoff_factor)
            if source.merge_date > (cutoff_dt - backoff):
                continue
            source.reminder_backoff_factor += 1

            # only keep the PRs which don't have an attached descendant)
            pr_ids = {p.id for p in prs}
            for pr in prs:
                pr_ids.discard(pr.parent_id.id)
            print(source, prs, [p.parent_id for p in prs],
                  '\n\t->', pr_ids, flush=True)
            for pr in (p for p in prs if p.id in pr_ids):
                self.env.ref('runbot_merge.forwardport.reminder')._send(
                    repository=pr.repository,
                    pull_request=pr.number,
                    token_field='fp_github_token',
                    format_args={'pr': pr, 'source': source},
                )

class Stagings(models.Model):
    _inherit = 'runbot_merge.stagings'

    def write(self, vals):
        r = super().write(vals)
        # we've just deactivated a successful staging (so it got ~merged)
        if vals.get('active') is False and self.state == 'success':
            # check all batches to see if they should be forward ported
            for b in self.with_context(active_test=False).batch_ids:
                # if all PRs of a batch have parents they're part of an FP
                # sequence and thus handled separately, otherwise they're
                # considered regular merges
                if not all(p.parent_id for p in b.prs):
                    self.env['forwardport.batches'].create({
                        'batch_id': b.id,
                        'source': 'merge',
                    })
        return r

class Feedback(models.Model):
    _inherit = 'runbot_merge.pull_requests.feedback'

    token_field = fields.Selection(selection_add=[('fp_github_token', 'Forwardport Bot')])


class CherrypickError(Exception):
    ...

class ForwardPortError(Exception):
    pass

def _clean_rename(s):
    """ Filters out the "inexact rename detection" spam of cherry-pick: it's
    useless but there seems to be no good way to silence these messages.
    """
    return '\n'.join(
        l for l in s.splitlines()
        if not l.startswith('Performing inexact rename detection')
    )

class HallOfShame(typing.NamedTuple):
    reviewers: list
    outstanding: list

class Outstanding(typing.NamedTuple):
    source: object
    prs: object
