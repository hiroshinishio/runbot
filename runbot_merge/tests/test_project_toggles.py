import pytest

from utils import Commit, to_pr

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
