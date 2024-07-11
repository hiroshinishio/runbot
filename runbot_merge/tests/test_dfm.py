from odoo.addons.runbot_merge.models.utils import dfm

def test_odoo_links():
    assert dfm("", "OPW-42") == '<p><a href="https://www.odoo.com/web#model=project.task&amp;id=42">opw-42</a></p>'
    assert dfm("", "taskid : 42") == '<p><a href="https://www.odoo.com/web#model=project.task&amp;id=42">task-42</a></p>'
    assert dfm("", "I was doing task foo") == '<p>I was doing task foo</p>'
    assert dfm("", "Task 687d3") == "<p>Task 687d3</p>"

def p(*content):
    return f'<p>{"".join(content)}</p>'
def a(label, url):
    return f'<a href="{url}">{label}</a>'
def test_gh_issue_links():
    # same-repository link
    assert dfm("odoo/runbot", "thing thing #26") == p("thing thing ", a('#26', 'https://github.com/odoo/runbot/issues/26'))
    assert dfm("odoo/runbot", "GH-26") == p(a('GH-26', 'https://github.com/odoo/runbot/issues/26'))
    assert dfm(
        "odoo/runbot", "https://github.com/odoo/runbot/issues/26"
    ) == p(a('#26', 'https://github.com/odoo/runbot/issues/26'))

    # cross-repo link
    assert dfm(
        "odoo/runbot", "jlord/sheetsee.js#26"
    ) == p(a('jlord/sheetsee.js#26', 'https://github.com/jlord/sheetsee.js/issues/26'))
    assert dfm(
        "odoo/runbot", "https://github.com/jlord/sheetsee.js/pull/26"
    ) == p(a('jlord/sheetsee.js#26', 'https://github.com/jlord/sheetsee.js/issues/26'))

    # cross-repo link with comment
    assert dfm(
        "odoo/runbot", "https://github.com/odoo/odoo/pull/173061#issuecomment-2227874482"
    ) == p(a("odoo/odoo#173061 (comment)", "https://github.com/odoo/odoo/issues/173061#issuecomment-2227874482"))


def test_gh_commit_link():
    # same repository
    assert dfm(
        "odoo/runbot", "https://github.com/odoo/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/odoo/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))
    # cross fork
    assert dfm(
        "odoo/runbot", "jlord@a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("jlord@a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/jlord/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))
    assert dfm(
        "odoo/runbot", "https://github.com/jlord/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("jlord@a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/jlord/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))
    # cross repo
    assert dfm(
        "odoo/runbot", "jlord/sheetsee.js@a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("jlord/sheetsee.js@a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/jlord/sheetsee.js/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))
    assert dfm(
        "odoo/runbot", "https://github.com/jlord/sheetsee.js/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("jlord/sheetsee.js@a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/jlord/sheetsee.js/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))

def test_standalone_hash():
    assert dfm(
        "odoo/runbot", "a5c3785ed8d6a35868bc169f07e40e889087fd2e"
    ) == p(a("a5c3785ed8d6a35868bc169f07e40e889087fd2e", "https://github.com/odoo/runbot/commit/a5c3785ed8d6a35868bc169f07e40e889087fd2e"))
    assert dfm(
        "odoo/runbot", "a5c3785ed8d6a35868bc169f07e4"
    ) == p(a("a5c3785ed8d6a35868bc169f07e4", "https://github.com/odoo/runbot/commit/a5c3785ed8d6a35868bc169f07e4"))
    assert dfm(
        "odoo/runbot", "a5c3785"
    ) == p(a("a5c3785", "https://github.com/odoo/runbot/commit/a5c3785"))
    assert dfm(
        "odoo/runbot", "a5c378"
    ) == p("a5c378")

def test_ignore_tel():
    assert dfm("", "[ok](https://github.com)") == p(a("ok", "https://github.com"))
    assert dfm("", "[nope](tel:+1-212-555-0100)") == "<p>nope</p>"
    assert dfm("", "[lol](rdar://10198949)") == "<p>lol</p>"
