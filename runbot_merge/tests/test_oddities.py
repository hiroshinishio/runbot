import pytest
import requests

from utils import Commit, to_pr, seen


def test_partner_merge(env):
    p_src = env['res.partner'].create({
        'name': "xxx",
        'github_login': 'xxx'
    })
    # proper login with useful info
    p_dest = env['res.partner'].create({
        'name': 'Partner P. Partnersson',
        'github_login': ''
    })

    env['base.partner.merge.automatic.wizard'].create({
        'state': 'selection',
        'partner_ids': (p_src + p_dest).ids,
        'dst_partner_id': p_dest.id,
    }).action_merge()
    assert not p_src.exists()
    assert p_dest.name == 'Partner P. Partnersson'
    assert p_dest.github_login == 'xxx'

def test_name_search(env):
    """ PRs should be findable by:

    * number
    * display_name (`repository#number`)
    * label

    This way we can find parents or sources by these informations.
    """
    p = env['runbot_merge.project'].create({
        'name': 'proj',
        'github_token': 'no',
    })
    b = env['runbot_merge.branch'].create({
        'name': 'target',
        'project_id': p.id
    })
    r = env['runbot_merge.repository'].create({
        'name': 'repo',
        'project_id': p.id,
    })

    baseline = {'target': b.id, 'repository': r.id}
    PRs = env['runbot_merge.pull_requests']
    prs = PRs.create({**baseline, 'number': 1964, 'label': 'victor:thump', 'head': 'a', 'message': 'x'})\
        | PRs.create({**baseline, 'number': 1959, 'label': 'marcus:frankenstein', 'head': 'b', 'message': 'y'})\
        | PRs.create({**baseline, 'number': 1969, 'label': 'victor:patch-1', 'head': 'c', 'message': 'z'})
    pr0, pr1, pr2 = prs.name_get()

    assert PRs.name_search('1964') == [pr0]
    assert PRs.name_search('1969') == [pr2]

    assert PRs.name_search('frank') == [pr1]
    assert PRs.name_search('victor') == [pr2, pr0]

    assert PRs.name_search('thump') == [pr0]

    assert PRs.name_search('repo') == [pr2, pr0, pr1]
    assert PRs.name_search('repo#1959') == [pr1]

def test_unreviewer(env, project, port):
    repo = env['runbot_merge.repository'].create({
        'project_id': project.id,
        'name': 'a_test_repo',
        'status_ids': [(0, 0, {'context': 'status'})]
    })
    p = env['res.partner'].create({
        'name': 'George Pearce',
        'github_login': 'emubitch',
        'review_rights': [(0, 0, {'repository_id': repo.id, 'review': True})]
    })

    r = requests.post(f'http://localhost:{port}/runbot_merge/get_reviewers', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {},
    })
    r.raise_for_status()
    assert 'error' not in r.json()
    assert r.json()['result'] == ['emubitch']

    r = requests.post(f'http://localhost:{port}/runbot_merge/remove_reviewers', json={
        'jsonrpc': '2.0',
        'id': None,
        'method': 'call',
        'params': {'github_logins': ['emubitch']},
    })
    r.raise_for_status()
    assert 'error' not in r.json()

    assert p.review_rights == env['res.partner.review']

def test_staging_post_update(env, repo, users, config):
    """Because statuses come from commits, it's possible to update the commits
    of a staging after that staging has completed (one way or the other), either
    by sending statuses directly (e.g. rebuilding, for non-deterministic errors)
    or just using the staging's head commit in a branch.

    This makes post-mortem analysis quite confusing, so stagings should
    "lock in" their statuses once they complete.
    """

    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing', tree={'m': 'c'}), ref='heads/other')
        pr = repo.make_pr(target='master', head='other')
        repo.post_status(pr.head, 'success')
        pr.post_comment('hansen r+ rebase-merge', config['role_reviewer']['token'])
    env.run_crons()
    pr_id = to_pr(env, pr)
    staging_id = pr_id.staging_id
    assert staging_id

    staging_head = repo.commit('staging.master')
    with repo:
        repo.post_status(staging_head, 'failure')
    env.run_crons()
    assert pr_id.state == 'error'
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'default', 'failure', ''],
    ]

    with repo:
        repo.post_status(staging_head, 'success')
    env.run_crons()
    assert staging_id.state == 'failure'
    assert staging_id.statuses == [
        [repo.name, 'default', 'failure', ''],
    ]

def test_merge_empty_commits(env, repo, users, config):
    """The mergebot should allow merging already-empty commits.
    """
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref='heads/master')

        repo.make_commits(m, Commit('thing1', tree={}), ref='heads/other1')
        pr1 = repo.make_pr(target='master', head='other1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

        repo.make_commits(m, Commit('thing2', tree={}), ref='heads/other2')
        pr2 = repo.make_pr(target='master', head='other2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])
    env.run_crons()
    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    assert pr1_id.staging_id and pr2_id.staging_id

    with repo:
        repo.post_status('staging.master', 'success')
    env.run_crons()

    assert pr1_id.state == pr2_id.state == 'merged'

    # log is most-recent-first (?)
    commits = list(repo.log('master'))
    head = repo.commit(commits[0]['sha'])
    assert repo.read_tree(head) == {'m': 'm'}

    assert commits[0]['commit']['message'].startswith('thing2')
    assert commits[1]['commit']['message'].startswith('thing1')
    assert commits[2]['commit']['message'] == 'initial'


def test_merge_emptying_commits(env, repo, users, config):
    """The mergebot should *not* allow merging non-empty commits which become
    empty as part of the staging (rebasing)
    """
    with repo:
        [m, _] = repo.make_commits(
            None,
            Commit('initial', tree={'m': 'm'}),
            Commit('second', tree={'m': 'c'}),
            ref='heads/master',
        )

        [c1] = repo.make_commits(m, Commit('thing', tree={'m': 'c'}), ref='heads/branch1')
        pr1 = repo.make_pr(target='master', head='branch1')
        repo.post_status(pr1.head, 'success')
        pr1.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])

        [_, c2] = repo.make_commits(
            m,
            Commit('thing1', tree={'c': 'c'}),
            Commit('thing2', tree={'m': 'c'}),
            ref='heads/branch2',
        )
        pr2 = repo.make_pr(target='master', head='branch2')
        repo.post_status(pr2.head, 'success')
        pr2.post_comment('hansen r+ rebase-ff', config['role_reviewer']['token'])

        repo.make_commits(
            m,
            Commit('thing1', tree={'m': 'x'}),
            Commit('thing2', tree={'m': 'c'}),
            ref='heads/branch3',
        )
        pr3 = repo.make_pr(target='master', head='branch3')
        repo.post_status(pr3.head, 'success')
        pr3.post_comment('hansen r+ squash', config['role_reviewer']['token'])
    env.run_crons()

    ping = f"@{users['user']} @{users['reviewer']}"
    # check that first / sole commit emptying is caught
    pr1_id = to_pr(env, pr1)
    assert not pr1_id.staging_id
    assert pr1.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c1} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert pr1_id.error
    assert pr1_id.state == 'error'

    # check that followup commit emptying is caught
    pr2_id = to_pr(env, pr2)
    assert not pr2_id.staging_id
    assert pr2.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c2} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert pr2_id.error
    assert pr2_id.state == 'error'

    # check that emptied squashed pr is caught
    pr3_id = to_pr(env, pr3)
    assert not pr3_id.staging_id
    assert pr3.comments[3:] == [
        (users['user'], f"{ping} unable to stage: results in an empty tree when merged, might be the duplicate of a merged PR.")
    ]
    assert pr3_id.error
    assert pr3_id.state == 'error'

    # ensure the PR does not get re-staged since it's the first of the staging
    # (it's the only one)
    env.run_crons()
    assert pr1.comments[3:] == [
        (users['user'], f"{ping} unable to stage: commit {c1} results in an empty tree when merged, it is likely a duplicate of a merged commit, rebase and remove.")
    ]
    assert len(pr2.comments) == 4
    assert len(pr3.comments) == 4

def test_force_ready(env, repo, config):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m': 'c1'}), ref="heads/other")
        pr = repo.make_pr(target='master', head='other')
    env.run_crons()

    pr_id = to_pr(env, pr)
    pr_id.skipchecks = True

    assert pr_id.state == 'ready'
    assert pr_id.status == 'pending'
    reviewer = env['res.users'].browse([env._uid]).partner_id
    assert pr_id.reviewed_by == reviewer

def test_help(env, repo, config, users, partners):
    with repo:
        [m] = repo.make_commits(None, Commit('initial', tree={'m': 'm'}), ref="heads/master")

        repo.make_commits(m, Commit('first', tree={'m': 'c1'}), ref="heads/other")
        pr = repo.make_pr(target='master', head='other')
    env.run_crons()

    for role in ['reviewer', 'self_reviewer', 'user', 'other']:
        v = config[f'role_{role}']
        with repo:
            pr.post_comment("hansen help", v['token'])
    with repo:
        pr.post_comment("hansen r+ help", config['role_reviewer']['token'])

    assert not partners['reviewer'].user_ids, "the reviewer should not be an internal user"

    group_internal = env.ref("base.group_user")
    group_admin = env.ref("runbot_merge.group_admin")
    env['res.users'].create({
        'partner_id': partners['reviewer'].id,
        'login': 'reviewer',
        'groups_id': [(4, group_internal.id, 0), (4, group_admin.id, 0)],
    })

    with repo:
        pr.post_comment("hansen help", config['role_reviewer']['token'])
    env.run_crons()

    assert pr.comments == [
        seen(env, pr, users),
        (users['reviewer'], "hansen help"),
        (users['self_reviewer'], "hansen help"),
        (users['user'], "hansen help"),
        (users['other'], "hansen help"),
        (users['reviewer'], "hansen r+ help"),
        (users['reviewer'], "hansen help"),
        (users['user'], REVIEWER.format(user=users['reviewer'], skip="")),
        (users['user'], RANDO.format(user=users['self_reviewer'])),
        (users['user'], AUTHOR.format(user=users['user'])),
        (users['user'], RANDO.format(user=users['other'])),
        (users['user'],
         REVIEWER.format(user=users['reviewer'], skip='')
         + "\n\nWarning: in invoking help, every other command has been ignored."),
        (users['user'], REVIEWER.format(
            user=users['reviewer'],
            skip='|`skipchecks`|bypasses both statuses and review|\n',
        )),
    ]

REVIEWER = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|
|`r(eview)+`|approves the PR, if it's a forwardport also approves all non-detached parents|
|`r(eview)=<number>`|only approves the specified parents|
|`fw=no`|does not forward-port this PR|
|`fw=default`|forward-ports this PR normally|
|`fw=skipci`|does not wait for a forward-port's statuses to succeed before creating the next one|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`merge`|integrate the PR with a simple merge commit, using the PR description as message|
|`rebase-merge`|rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message|
|`rebase-ff`|rebases the PR on top of the target branch, then fast-forwards|
|`squash`|squashes the PR as a single commit on the target branch, using the PR description as message|
|`delegate+`|grants approval rights to the PR author|
|`delegate=<...>`|grants approval rights on this PR to the specified github users|
|`default`|stages the PR normally|
|`priority`|tries to stage this PR first, then adds `default` PRs if the staging has room|
|`alone`|stages this PR only with other PRs of the same priority|
{skip}\
|`cancel=staging`|automatically cancels the current staging when this PR becomes ready|
|`check`|fetches or refreshes PR metadata, resets mergebot state|

Note: this help text is dynamic and will change with the state of the PR.\
"""
AUTHOR = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|
|`fw=no`|does not forward-port this PR|
|`up to <branch>`|only ports this PR forward to the specified branch (included)|
|`check`|fetches or refreshes PR metadata, resets mergebot state|

Note: this help text is dynamic and will change with the state of the PR.\
"""
RANDO = """\
Currently available commands for @{user}:

|command||
|-|-|
|`help`|displays this help|

Note: this help text is dynamic and will change with the state of the PR.\
"""
