"""This module tests edge cases specific to the batch objects themselves,
without wider relevance and thus other location.
"""
import pytest

from utils import Commit, to_pr, pr_page


def test_close_single(env, repo):
    """If a batch has a single PR and that PR gets closed, the batch should be
    inactive *and* blocked.
    """
    with repo:
        repo.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        [c] = repo.make_commits('master', Commit('b', tree={"b": "b"}))
        pr = repo.make_pr(head=c, target='master')
    env.run_crons()

    pr_id = to_pr(env, pr)
    batch_id = pr_id.batch_id
    assert pr_id.state == 'opened'
    assert batch_id.blocked
    Batches = env['runbot_merge.batch']
    assert Batches.search_count([]) == 1

    with repo:
        pr.close()

    assert pr_id.state == 'closed'
    assert batch_id.all_prs == pr_id
    assert batch_id.prs == pr_id.browse(())
    assert batch_id.blocked == "all prs are closed"
    assert not batch_id.active

    assert Batches.search_count([]) == 0

def test_close_multiple(env, make_repo2):
    Batches = env['runbot_merge.batch']
    repo1 = make_repo2('wheee')
    repo2 = make_repo2('wheeee')

    with repo1:
        repo1.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        repo1.make_commits('master', Commit('b', tree={"b": "b"}), ref='heads/a_pr')
        pr1 = repo1.make_pr(head='a_pr', target='master')

    with repo2:
        repo2.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        repo2.make_commits('master', Commit('b', tree={"b": "b"}), ref='heads/a_pr')
        pr2 = repo2.make_pr(head='a_pr', target='master')

    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    batch_id = pr1_id.batch_id
    assert pr2_id.batch_id == batch_id

    assert pr1_id.state == 'opened'
    assert pr2_id.state == 'opened'
    assert batch_id.all_prs == pr1_id | pr2_id
    assert batch_id.prs == pr1_id | pr2_id
    assert batch_id.active
    assert Batches.search_count([]) == 1

    with repo1:
        pr1.close()

    assert pr1_id.state == 'closed'
    assert pr2_id.state == 'opened'
    assert batch_id.all_prs == pr1_id | pr2_id
    assert batch_id.prs == pr2_id
    assert batch_id.active
    assert Batches.search_count([]) == 1

    with repo2:
        pr2.close()

    assert pr1_id.state == 'closed'
    assert pr2_id.state == 'closed'
    assert batch_id.all_prs == pr1_id | pr2_id
    assert batch_id.prs == env['runbot_merge.pull_requests'].browse(())
    assert not batch_id.active
    assert Batches.search_count([]) == 0

def test_inconsistent_target(env, project, make_repo2, users, page, config):
    """If a batch's PRs have inconsistent targets,

    - only open PRs should count
    - it should be clearly notified on the dash
    - the dash should not get hopelessly lost
    - there should be a wizard to split the batch / move a PR to a separate batch
    """
    # region setup
    Batches = env['runbot_merge.batch']
    repo1 = make_repo2('whe')
    repo2 = make_repo2('whee')
    repo3 = make_repo2('wheee')
    project.write({'branch_ids': [(0, 0, {'name': 'other'})]})

    with repo1:
        [m] = repo1.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        repo1.make_ref('heads/other', m)
        repo1.make_commits('master', Commit('b', tree={"b": "b"}), ref='heads/a_pr')
        pr1 = repo1.make_pr(head='a_pr', target='master')

        repo1.make_commits('master', Commit('b', tree={"c": "c"}), ref='heads/something_else')
        pr_other = repo1.make_pr(head='something_else', target='master')

    with repo2:
        repo2.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        repo2.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/other')
        repo2.make_commits('master', Commit('b', tree={"b": "b"}), ref='heads/a_pr')
        pr2 = repo2.make_pr(head='a_pr', target='master')

    with repo3:
        repo3.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/master')
        repo3.make_commits(None, Commit("a", tree={"a": "a"}), ref='heads/other')
        repo3.make_commits('master', Commit('b', tree={"b": "b"}), ref='heads/a_pr')
        pr3 = repo3.make_pr(head='a_pr', target='master')

    assert repo1.owner == repo2.owner == repo3.owner
    owner = repo1.owner
    # endregion

    # region closeable consistency

    [b] = Batches.search([('all_prs.label', '=', f'{owner}:a_pr')])
    assert b.target.name == 'master'
    assert len(b.prs) == 3
    assert len(b.all_prs) == 3

    with repo3:
        pr3.base = 'other'
    assert b.target.name == False
    assert len(b.prs) == 3
    assert len(b.all_prs) == 3

    with repo3:
        pr3.close()
    assert b.target.name == 'master'
    assert len(b.prs) == 2
    assert len(b.all_prs) == 3
    # endregion

    # region split batch
    pr1_id = to_pr(env, pr1)
    pr2_id = to_pr(env, pr2)
    with repo2:
        pr2.base = 'other'

    pr2_dashboard = pr_page(page, pr2)
    # The dashboard should have an alert
    s = pr2_dashboard.cssselect('.alert.alert-danger')
    assert s, "the dashboard should have an alert"
    assert s[0].text_content().strip() == f"""\
Inconsistent targets:

{pr1_id.display_name} has target 'master'
{pr2_id.display_name} has target 'other'\
"""
    assert not pr2_dashboard.cssselect('table'), "the batches table should be suppressed"

    assert b.target.name == False
    assert to_pr(env, pr_other).label == f'{owner}:something_else'
    # try staging
    with repo1:
        pr1.post_comment("hansen r+", config['role_reviewer']['token'])
        repo1.post_status(pr1.head, "success")
    with repo2:
        pr2.post_comment("hansen r+", config['role_reviewer']['token'])
        repo2.post_status(pr2.head, "success")
    env.run_crons()
    assert not pr1_id.blocked
    assert not pr2_id.blocked
    assert b.blocked
    assert env['runbot_merge.stagings'].search_count([]) == 0

    act = pr2_id.button_split()
    assert act['type'] == 'ir.actions.act_window'
    assert act['views'] == [[False, 'form']]
    assert act['target'] == 'new'
    w = env[act['res_model']].browse([act['res_id']])
    w.new_label = f"{owner}:something_else"
    with pytest.raises(Exception):
        w.button_apply()
    w.new_label = f"{owner}:blah-blah-blah"
    w.button_apply()

    assert pr2_id.label == f"{owner}:blah-blah-blah"
    assert pr2_id.batch_id != to_pr(env, pr1).batch_id
    assert b.target.name == 'master'
    assert len(b.prs) == 1, "the PR has been moved off of this batch entirely"
    assert len(b.all_prs) == 2
    # endregion

    assert not pr1_id.blocked
    assert not pr1_id.batch_id.blocked
    assert not pr2_id.blocked
    assert not pr2_id.batch_id.blocked
    env.run_crons()

    assert env['runbot_merge.stagings'].search_count([])
