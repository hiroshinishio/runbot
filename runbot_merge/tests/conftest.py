import pytest

@pytest.fixture()
def module():
    return 'runbot_merge'

@pytest.fixture
def default_crons():
    return [
        # env['runbot_merge.project']._check_fetch()
        'runbot_merge.fetch_prs_cron',
        # env['runbot_merge.commit']._notify()
        'runbot_merge.process_updated_commits',
        # env['runbot_merge.project']._check_stagings()
        'runbot_merge.merge_cron',
        # env['runbot_merge.project']._create_stagings()
        'runbot_merge.staging_cron',
        # env['runbot_merge.pull_requests']._check_linked_prs_statuses()
        'runbot_merge.check_linked_prs_status',
        # env['runbot_merge.pull_requests.feedback']._send()
        'runbot_merge.feedback_cron',
    ]

@pytest.fixture
def project(env, config):
    return env['runbot_merge.project'].create({
        'name': 'odoo',
        'github_token': config['github']['token'],
        'github_prefix': 'hansen',
        'branch_ids': [(0, 0, {'name': 'master'})],
    })


@pytest.fixture
def make_repo2(env, project, make_repo, users, setreviewers):
    """Layer over ``make_repo`` which also:

    - adds the new repo to ``project`` (with no group and the ``'default'`` status required)
    - sets the standard reviewers on the repo
    - and creates an event source for the repo
    """
    def mr(name):
        r = make_repo(name)
        rr = env['runbot_merge.repository'].create({
            'project_id': project.id,
            'name': r.name,
            'group_id': False,
            'required_statuses': 'default',
        })
        setreviewers(rr)
        env['runbot_merge.events_sources'].create({'repository': r.name})
        return r
    return mr


@pytest.fixture
def repo(make_repo2):
    return make_repo2('repo')
