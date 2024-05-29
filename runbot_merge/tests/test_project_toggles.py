import functools
from itertools import repeat

import pytest

from utils import Commit, to_pr, ensure_one


@pytest.fixture
def repo(env, project, make_repo, users, setreviewers):
    r = make_repo('repo')
    project.write({'repo_ids': [(0, 0, {
        'name': r.name,
        'group_id': False,
        'required_statuses': 'default',
    })]})
    setreviewers(*project.repo_ids)
    return r

def test_disable_staging(env, project, repo, config):
    """In order to avoid issues of cron locking, as well as not disable staging
    for every project when trying to freeze just one of them (cough cough), a
    toggle is available on the project to skip staging for it.
    """
    with repo:
        [m] = repo.make_commits(None, Commit("m", tree={"a": "1"}), ref="heads/master")

        [c] = repo.make_commits(m, Commit("c", tree={"a": "2"}), ref="heads/other")
        pr = repo.make_pr(title="whatever", target="master", head="other")
        pr.post_comment("hansen r+", config["role_reviewer"]['token'])
        repo.post_status(c, "success")
    env.run_crons()

    pr_id = to_pr(env, pr)
    staging_1 = pr_id.staging_id
    assert staging_1.active

    project.staging_enabled = False
    staging_1.cancel("because")

    env.run_crons()

    assert staging_1.active is False
    assert staging_1.state == "cancelled"
    assert not pr_id.staging_id.active,\
        "should not be re-staged, because staging has been disabled"

@pytest.mark.parametrize('mode,cutoff,second', [
    # default mode, the second staging is the first half of the first staging
    ('default', 2, [0]),
    # splits are right-biased (the midpoint is rounded down), so for odd
    # staging sizes the first split is the smaller one
    ('default', 3, [0]),
    # if the split results in ((1, 2), 1), largest stages the second
    ('largest', 3, [1, 2]),
    # if the split results in ((1, 1), 2), largest stages the ready PRs
    ('largest', 2, [2, 3]),
    # even if it's a small minority, ready selects the ready PR(s)
    ('ready', 3, [3]),
    ('ready', 2, [2, 3]),
])
def test_staging_priority(env, project, repo, config, mode, cutoff, second):
    """By default, unless a PR is prioritised as "alone" splits take priority
    over new stagings.

    *However* to try and maximise throughput in trying times, it's possible to
    configure the project to prioritise either the largest staging (between spit
    and ready batches), or to just prioritise new stagings.
    """
    def select(prs, indices):
        zero = env['runbot_merge.pull_requests']
        filtered = (p for i, p in enumerate(prs) if i in indices)
        return functools.reduce(lambda a, b: a | b, filtered, zero)

    project.staging_priority = mode
    # we need at least 3 PRs, two that we can split out, and one leftover
    with repo:
        [m] = repo.make_commits(None, Commit("m", tree={"ble": "1"}), ref="heads/master")

        [c] = repo.make_commits(m, Commit("c", tree={"1": "1"}), ref="heads/pr1")
        pr1 = repo.make_pr(title="whatever", target="master", head="pr1")

        [c] = repo.make_commits(m, Commit("c", tree={"2": "2"}), ref="heads/pr2")
        pr2 = repo.make_pr(title="whatever", target="master", head="pr2")

        [c] = repo.make_commits(m, Commit("c", tree={"3": "3"}), ref="heads/pr3")
        pr3 = repo.make_pr(title="whatever", target="master", head="pr3")

        [c] = repo.make_commits(m, Commit("c", tree={"4": "4"}), ref="heads/pr4")
        pr4 = repo.make_pr(title="whatever", target="master", head="pr4")

    prs = [pr1, pr2, pr3, pr4]
    pr_ids = functools.reduce(
        lambda a, b: a | b,
        map(to_pr, repeat(env), prs)
    )
    # ready the PRs for the initial staging (to split)
    pre_cutoff = pr_ids[:cutoff]
    with repo:
        for pr, pr_id in zip(prs[:cutoff], pre_cutoff):
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
            repo.post_status(pr_id.head, 'success')
    env.run_crons()
    # check they staged as expected
    assert all(p.staging_id for p in pre_cutoff)
    staging = ensure_one(env['runbot_merge.stagings'].search([]))
    ensure_one(pre_cutoff.staging_id)

    # ready the rest
    with repo:
        for pr, pr_id in zip(prs[cutoff:], pr_ids[cutoff:]):
            pr.post_comment('hansen r+', config['role_reviewer']['token'])
            repo.post_status(pr_id.head, 'success')
    env.run_crons('runbot_merge.process_updated_commits')
    assert not pr_ids.filtered(lambda p: p.blocked)

    # trigger a split
    with repo:
        repo.post_status('staging.master', 'failure')
    env.run_crons('runbot_merge.process_updated_commits', 'runbot_merge.merge_cron')
    assert not staging.active
    assert not env['runbot_merge.stagings'].search([]).active
    assert env['runbot_merge.split'].search_count([]) == 2

    env.run_crons()

    # check that st.pr_ids are the PRs we expect
    st = env['runbot_merge.stagings'].search([])
    assert st.pr_ids == select(pr_ids, second)
