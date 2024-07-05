import re

from utils import Commit, make_basic, to_pr, seen, matches


def test_single_updated(env, config, make_repo):
    """ Given co-dependent PRs getting merged, one of them being modified should
    lead to a restart of the merge & forward port process.

    See test_update_pr for a simpler (single-PR) version
    """
    r1, _ = make_basic(env, config, make_repo, reponame='repo-1')
    r2, _ = make_basic(env, config, make_repo, reponame='repo-2')

    with r1:
        r1.make_commits('a', Commit('1', tree={'1': '0'}), ref='heads/aref')
        pr1 = r1.make_pr(target='a', head='aref')
        r1.post_status('aref', 'success', 'legal/cla')
        r1.post_status('aref', 'success', 'ci/runbot')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])
    with r2:
        r2.make_commits('a', Commit('2', tree={'2': '0'}), ref='heads/aref')
        pr2 = r2.make_pr(target='a', head='aref')
        r2.post_status('aref', 'success', 'legal/cla')
        r2.post_status('aref', 'success', 'ci/runbot')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.a', 'success', 'legal/cla')
        r1.post_status('staging.a', 'success', 'ci/runbot')
        r2.post_status('staging.a', 'success', 'legal/cla')
        r2.post_status('staging.a', 'success', 'ci/runbot')
    env.run_crons()

    pr1_id, pr11_id, pr2_id, pr21_id = pr_ids = env['runbot_merge.pull_requests'].search([]).sorted('display_name')
    assert pr1_id.number == pr1.number
    assert pr2_id.number == pr2.number
    assert pr1_id.state == pr2_id.state == 'merged'

    assert pr11_id.parent_id == pr1_id
    assert pr11_id.repository.name == pr1_id.repository.name == r1.name

    assert pr21_id.parent_id == pr2_id
    assert pr21_id.repository.name == pr2_id.repository.name == r2.name

    assert pr11_id.target.name == pr21_id.target.name == 'b'

    # don't even bother faking CI failure, straight update pr21_id
    repo, ref = r2.get_pr(pr21_id.number).branch
    with repo:
        repo.make_commits(
            pr21_id.target.name,
            Commit('Whops', tree={'2': '1'}),
            ref='heads/' + ref,
            make=False
        )
    env.run_crons()

    assert not pr21_id.parent_id

    with r1, r2:
        r1.post_status(pr11_id.head, 'success', 'legal/cla')
        r1.post_status(pr11_id.head, 'success', 'ci/runbot')
        r1.get_pr(pr11_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
        r2.post_status(pr21_id.head, 'success', 'legal/cla')
        r2.post_status(pr21_id.head, 'success', 'ci/runbot')
        r2.get_pr(pr21_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    prs_again = env['runbot_merge.pull_requests'].search([])
    assert prs_again == pr_ids,\
        "should not have created FP PRs as we're now in a detached (iso new PR) state " \
        "(%s)" % prs_again.mapped('display_name')

    with r1, r2:
        r1.post_status('staging.b', 'success', 'legal/cla')
        r1.post_status('staging.b', 'success', 'ci/runbot')
        r2.post_status('staging.b', 'success', 'legal/cla')
        r2.post_status('staging.b', 'success', 'ci/runbot')
    env.run_crons()

    new_prs = env['runbot_merge.pull_requests'].search([]).sorted('display_name') - pr_ids
    assert len(new_prs) == 2, "should have created the new FP PRs"
    pr12_id, pr22_id = new_prs

    assert pr12_id.source_id == pr1_id
    assert pr12_id.parent_id == pr11_id

    assert pr22_id.source_id == pr2_id
    assert pr22_id.parent_id == pr21_id

def test_closing_during_fp(env, config, make_repo, users):
    """ Closing a PR after it's been ported once should not port it further, but
    the rest of the batch should carry on
    """
    r1, _ = make_basic(env, config, make_repo)
    r2, _ = make_basic(env, config, make_repo)
    env['runbot_merge.repository'].search([]).required_statuses = 'default'

    with r1, r2:
        r1.make_commits('a', Commit('1', tree={'1': '0'}), ref='heads/aref')
        pr1 = r1.make_pr(target='a', head='aref')
        r1.post_status('aref', 'success')
        pr1.post_comment('hansen r+', config['role_reviewer']['token'])

        r2.make_commits('a', Commit('2', tree={'2': '0'}), ref='heads/aref')
        pr2 = r2.make_pr(target='a', head='aref')
        r2.post_status('aref', 'success')
        pr2.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.a', 'success')
        r2.post_status('staging.a', 'success')
    env.run_crons()

    pr1_id = to_pr(env, pr1)
    [pr1_1_id] = pr1_id.forwardport_ids
    pr2_id = to_pr(env, pr2)
    [pr2_1_id] = pr2_id.forwardport_ids

    with r1:
        r1.get_pr(pr1_1_id.number).close(config['role_user']['token'])

    with r2:
        r2.post_status(pr2_1_id.head, 'success')
    env.run_crons()

    assert env['runbot_merge.pull_requests'].search_count([]) == 5,\
        "only one of the forward ports should be ported"
    assert not env['runbot_merge.pull_requests'].search([('parent_id', '=', pr1_1_id.id)]),\
        "the closed PR should not be ported"
    assert env['runbot_merge.pull_requests'].search([('source_id', '=', pr1_id.id)]) == pr1_1_id,\
        "the closed PR should not be ported"

    r1_b_head = r1.commit("b")
    with r2:
        r2.get_pr(pr2_1_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    assert not pr2_1_id.blocked
    assert not pr2_1_id.batch_id.blocked
    st = pr2_1_id.staging_id
    assert st
    with r1, r2:
        r1.post_status('staging.b', 'success')
        r2.post_status('staging.b', 'success')
    env.run_crons()
    assert st.state == 'success'

    assert r1_b_head.id == r1.commit("b").id, \
        "r1:b's head should not have been touched"

def test_add_pr_during_fp(env, config, make_repo, users):
    """ It should be possible to add new PRs to an FP batch
    """
    r1, _ = make_basic(env, config, make_repo, statuses="default")
    r2, fork2 = make_basic(env, config, make_repo, statuses="default")
    # needs a "d" branch
    env['runbot_merge.project'].search([]).write({
        'branch_ids': [(0, 0, {'name': 'd', 'sequence': 40})],
    })
    with r1, r2:
        r1.make_ref("heads/d", r1.commit("c").id)
        r2.make_ref("heads/d", r2.commit("c").id)

    with r1:
        r1.make_commits('a', Commit('1', tree={'1': '0'}), ref='heads/aref')
        pr1_a = r1.make_pr(target='a', head='aref')
        r1.post_status('aref', 'success')
        pr1_a.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.a', 'success')
        r2.post_status('staging.a', 'success')
    env.run_crons()

    pr1_a_id = to_pr(env, pr1_a)
    [pr1_b_id] = pr1_a_id.forwardport_ids

    with r2, fork2:
        fork2.make_commits('b', Commit('2', tree={'2': '0'}), ref=f'heads/{pr1_b_id.refname}')
        pr2_b = r2.make_pr(title="B", target='b', head=f'{fork2.owner}:{pr1_b_id.refname}')
    env.run_crons()

    pr2_b_id = to_pr(env, pr2_b)

    assert not pr1_b_id.staging_id
    assert not pr2_b_id.staging_id
    assert pr1_b_id.batch_id == pr2_b_id.batch_id
    assert pr1_b_id.state == "opened",\
        "implicit approval from forward port should have been canceled"
    batch = pr2_b_id.batch_id

    with r1:
        r1.post_status(pr1_b_id.head, 'success')
        r1.get_pr(pr1_b_id.number).post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()

    assert batch.blocked
    assert pr1_b_id.blocked

    with r2:
        r2.post_status(pr2_b.head, "success")
        pr2_b.post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()

    assert not batch.blocked
    assert pr1_b_id.staging_id and pr1_b_id.staging_id == pr2_b_id.staging_id

    with r1, r2:
        r1.post_status('staging.b', 'success')
        r2.post_status('staging.b', 'success')
    env.run_crons()

    def find_child(pr):
        return env['runbot_merge.pull_requests'].search([
            ('parent_id', '=', pr.id),
        ])
    pr1_c_id = find_child(pr1_b_id)
    assert pr1_c_id
    pr2_c_id = find_child(pr2_b_id)
    assert pr2_c_id

    with r1, r2:
        r1.post_status(pr1_c_id.head, 'success')
        r2.post_status(pr2_c_id.head, 'success')
    env.run_crons()

    assert find_child(pr1_c_id)
    assert find_child(pr2_c_id)

def test_add_to_forward_ported(env, config, make_repo, users):
    """Add a new branch to an intermediate step of a fw *sequence*, either
    because skipci or because all the intermediate CI succeeded
    """
    # region setup
    r1, _ = make_basic(env, config, make_repo, statuses="default")
    r2, fork2 = make_basic(env, config, make_repo, statuses="default")

    with r1:
        r1.make_commits('a', Commit('a', tree={'a': 'a'}), ref="heads/pr1")
        pr1_a = r1.make_pr(target="a", head="pr1")
        r1.post_status(pr1_a.head, 'success')
        pr1_a.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with r1, r2:
        r1.post_status('staging.a', 'success')
        r2.post_status('staging.a', 'success')
    env.run_crons()

    # region port forward
    pr1_a_id = to_pr(env, pr1_a)
    pr1_b_id = pr1_a_id.forwardport_ids
    assert pr1_b_id
    with r1:
        r1.post_status(pr1_b_id.head, 'success')
    env.run_crons()
    pr1_c_id = pr1_a_id.forwardport_ids - pr1_b_id
    assert pr1_c_id
    # endregion
    # endregion

    # new PR must be in fork for labels to actually match
    with r2, fork2:
        # branch in fork has no owner prefix, but HEAD for cross-repo PR does
        fork2.make_commits("b", Commit('b', tree={'b': 'b'}), ref=f'heads/{pr1_b_id.refname}')
        pr2_b = r2.make_pr(title="b", target="b", head=pr1_b_id.label)
        r2.post_status(pr2_b.head, 'success')
    env.run_crons()

    pr2_b_id = to_pr(env, pr2_b)
    assert pr2_b_id.batch_id == pr1_b_id.batch_id
    assert len(pr2_b_id.forwardport_ids) == 1, \
        "since the batch is already forward ported, the new PR should" \
         " immediately be forward ported to match"
    assert pr2_b_id.forwardport_ids.label == pr1_c_id.label

    pr2_a = r1.get_pr(pr1_b_id.number)
    with r1, r2:
        pr2_a.post_comment('hansen r+', config['role_reviewer']['token'])
        pr2_b.post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.b', 'success')
        r2.post_status('staging.b', 'success')
    env.run_crons()

    assert pr1_b_id.state == 'merged'
    assert pr2_b_id.state == 'merged'

    assert len(pr2_b_id.forwardport_ids) == 1,\
        "verify that pr2_b did not get forward ported again on merge"
    pr2_c = r2.get_pr(pr2_b_id.forwardport_ids.number)
    assert pr2_c.comments == [
        seen(env, pr2_c, users),
        (users['user'], '''\
@{user} this PR targets c and is the last of the forward-port chain.

To merge the full chain, use
> @hansen r+

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
'''.format_map(users)),
    ]

def test_add_to_forward_port_conflict(env, config, make_repo, users):
    """If a PR is added to an existing forward port sequence, and it causes
    conflicts when forward ported, it should be treated similarly to an *update*
    causing a conflict: the PR is still created, but it's set in conflict.
    """
    # region setup
    r1, _ = make_basic(env, config, make_repo, statuses="default")
    r2, fork2 = make_basic(env, config, make_repo, statuses="default")
    project = env['runbot_merge.project'].search([])
    with r2:
        r2.make_commits(
            "c",
            Commit("C-onflict", tree={"b": "X"}),
            ref="heads/c"
        )

    with r1:
        r1.make_commits('a', Commit('a', tree={'a': 'a'}), ref="heads/pr1")
        pr1_a = r1.make_pr(target="a", head="pr1")
        r1.post_status(pr1_a.head, 'success')
        pr1_a.post_comment('hansen r+', config['role_reviewer']['token'])
    env.run_crons()
    with r1, r2:
        r1.post_status('staging.a', 'success')
        r2.post_status('staging.a', 'success')
    env.run_crons()

    # region port forward
    pr1_a_id = to_pr(env, pr1_a)
    pr1_b_id = pr1_a_id.forwardport_ids
    assert pr1_b_id
    with r1:
        r1.post_status(pr1_b_id.head, 'success')
    env.run_crons()
    pr1_c_id = pr1_a_id.forwardport_ids - pr1_b_id
    assert pr1_c_id
    # endregion
    # endregion

    # new PR must be in fork for labels to actually match
    with r2, fork2:
        # branch in fork has no owner prefix, but HEAD for cross-repo PR does
        fork2.make_commits("b", Commit('b', tree={'b': 'b'}), ref=f'heads/{pr1_b_id.refname}')
        pr2_b = r2.make_pr(title="b", target="b", head=pr1_b_id.label)
        r2.post_status(pr2_b.head, 'success')
    env.run_crons()

    pr2_b_id = to_pr(env, pr2_b)
    assert pr2_b_id.batch_id == pr1_b_id.batch_id
    pr2_c_id = pr2_b_id.forwardport_ids
    assert len(pr2_c_id) == 1, \
        "since the batch is already forward ported, the new PR should" \
         " immediately be forward ported to match"
    assert pr2_c_id.label == pr1_c_id.label
    assert not pr2_c_id.parent_id, "conflict -> should be detached"
    assert pr2_c_id.detach_reason

    pr2_a = r1.get_pr(pr1_b_id.number)
    with r1, r2:
        pr2_a.post_comment('hansen r+', config['role_reviewer']['token'])
        pr2_b.post_comment("hansen r+", config['role_reviewer']['token'])
    env.run_crons()

    with r1, r2:
        r1.post_status('staging.b', 'success')
        r2.post_status('staging.b', 'success')
    env.run_crons()

    assert pr1_b_id.state == 'merged'
    assert pr2_b_id.state == 'merged'

    pr2_c = r2.get_pr(pr2_c_id.number)
    assert pr2_c.comments == [
        seen(env, pr2_c, users),
        # should have conflicts
        (users['user'], """@{user} cherrypicking of pull request {previous.display_name} failed.

stdout:
```
Auto-merging b
CONFLICT (add/add): Merge conflict in b

```

Either perform the forward-port manually (and push to this branch, proceeding as usual) or close this PR (maybe?).

In the former case, you may want to edit this PR message as well.

:warning: after resolving this conflict, you will need to merge it via @{project.github_prefix}.

More info at https://github.com/odoo/odoo/wiki/Mergebot#forward-port
""".format(project=project, previous=pr2_b_id, **users))
    ]
