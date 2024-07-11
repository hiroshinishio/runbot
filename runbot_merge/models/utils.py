import logging
from contextvars import ContextVar
from typing import Tuple
from xml.etree.ElementTree import Element, tostring

import markdown.inlinepatterns
import markdown.treeprocessors
from markupsafe import escape, Markup


def enum(model: str, field: str) -> Tuple[str, str]:
    n = f'{model.replace(".", "_")}_{field}_type'
    return n, n


def readonly(_):
    raise TypeError("Field is readonly")


DFM_CONTEXT_REPO = ContextVar("dfm_context", default="")
def dfm(repository: str, text: str) -> Markup:
    """ Converts the input text from markup to HTML using the Odoo PR
    Description Rules, which are basically:

    - GFM
    - minus raw HTML (?)
    - + github's autolinking (https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/autolinked-references-and-urls)
    - + bespoke autolinking of OPW and Task links to odoo.com
    """
    t = DFM_CONTEXT_REPO.set(repository)
    try:
        return Markup(dfm_renderer.convert(escape(text)))
    finally:
        DFM_CONTEXT_REPO.reset(t)


class DfmExtension(markdown.extensions.Extension):
    def extendMarkdown(self, md):
        md.registerExtensions(['fenced_code', 'footnotes', 'nl2br', 'sane_lists', 'tables'], configs={})
        md.inlinePatterns.register(GithubLinking(md), 'githublinking', 123)
        md.inlinePatterns.register(OdooLinking(md), 'odoolinking', 124)
        # ideally the unlinker should run before the prettifier so the
        # prettification is done correctly, but it seems unlikely the prettifier
        # handles the variable nature of links correctly, and we likely want to
        # run after the unescaper
        md.treeprocessors.register(Unlinker(), "unlinker", -10)

class GithubLinking(markdown.inlinepatterns.InlineProcessor):
    """Aside from being *very* varied github links are *contextual*. That is,
    their resolution depends on the repository they're being called from
    (technically they also need all the information from the github backend to
    know the people & objects exist but we don't have that option).

    Context is not available to us, but we can fake it through the application
    of contextvars: ``DFM_CONTEXT_REPO`` should contain the full name of the
    repository this is being resolved from.

    If ``DFM_CONTEXT_REPO`` is empty and needed, this processor emits a warning.
    """
    def __init__(self, md=None):
        super().__init__(r"""(?xi)
(?:
    \bhttps://github.com/([\w\.-]+/[\w\.-]+)/(?:issues|pull)/(\d+)(\#[\w-]+)?
|   \bhttps://github.com/([\w\.-]+/[\w\.-]+)/commit/([a-f0-9]+)
|   \b([\w\.-]+/[\w\.-]+)\#(\d+)
|   (\bGH-|(?:^|(?<=\s))\#)(\d+)
|   \b(?:
        # user@sha or user/repo@sha
        ([\w\.-]+(?:/[\w\.-]+)?)
        @
        ([0-9a-f]{7,40})
    )
|   \b(
        # a sha is 7~40 hex digits but that means any million+ number matches
        # which is probably wrong. So ensure there's at least one letter in the
        # set by using a positive lookahead which looks for a sequence of at
        # least 0 numbers followed by a-f
        (?=[0-9]{0,39}?[a-f])
        [0-9a-f]{7,40}
    )
)
\b
""", md)

    def handleMatch(self, m, data):
        ctx = DFM_CONTEXT_REPO.get()
        if not ctx:
            logging.getLogger(__name__)\
                .getChild("github_links")\
                .warning("missing context for rewriting github links, skipping")
            return m[0], *m.span()

        repo = issue = commit = None
        if m[2]:  # full issue / PR
            repo = m[1]
            issue = m[2]
        elif m[5]:  # long hash
            repo = m[4]
            commit = m[5]
        elif m[7]:  # short issue with repo
            repo = m[6]
            issue = m[7]
        elif m[9]:  # short issue without repo
            repo = None if m[8] == '#' else "GH"
            issue = m[9]
        elif m[11]:  # medium hash
            repo = m[10]
            commit = m[11]
        else:  # hash only
            commit = m[12]

        el = Element("a")
        if issue is not None:
            if repo == "GH":
                el.text = f"GH-{issue}"
                repo = ctx
            elif repo in (None, ctx):
                repo = ctx
                el.text = f"#{issue}"
            else:
                el.text = f"{repo}#{issue}"

            if (fragment := m[3]) and fragment.startswith('#issuecomment-'):
                el.text += ' (comment)'
            else:
                fragment = ''
            el.set('href', f"https://github.com/{repo}/issues/{issue}{fragment}")
        else:
            if repo in (None, ctx):
                label_repo = ""
                repo = ctx
            elif '/' not in repo:  # owner-only
                label_repo = repo
                # NOTE: I assume in reality we're supposed to find the actual fork if unambiguous...
                repo = repo + '/' + ctx.split('/')[-1]
            elif repo.split('/')[-1] == ctx.split('/')[-1]:
                # NOTE: here we assume if it's the same repo in a different owner it's a fork
                label_repo = repo.split('/')[0]
            else:
                label_repo = repo
            el.text = f"{label_repo}@{commit}" if label_repo else commit
            el.set("href", f"https://github.com/{repo}/commit/{commit}")
        return el, *m.span()


class OdooLinking(markdown.inlinepatterns.InlineProcessor):
    def __init__(self, md=None):
        # there are other weirder variations but fuck em, this matches
        # "opw", "task", "task-id" or "taskid" followed by an optional - or :
        # followed by digits
        super().__init__(r"(?i)\b(task(?:-?id)?|opw)\s*[-:]?\s*(\d+)\b", md)

    def handleMatch(self, m, data):
        el = Element("a", href='https://www.odoo.com/web#model=project.task&id=' + m[2])
        if m[1].lower() == 'opw':
            el.text = f"opw-{m[2]}"
        else:
            el.text = f"task-{m[2]}"
        return el, *m.span()


class Unlinker(markdown.treeprocessors.Treeprocessor):
    def run(self, root):
        # find all elements which contain a link, as ElementTree does not have
        # parent links we can't really replace links in place
        for parent in root.iterfind('.//*[a]'):
            children = parent[:]
            # can't use clear because that clears the attributes and tail/text
            del parent[:]
            for el in children:
                if el.tag != 'a' or el.get('href', '').startswith(('https:', 'http:')):
                    parent.append(el)
                    continue

                # this is a weird link, remove it

                if el.text:  # first attach its text to the previous element
                    if len(parent):  # prev is not parent
                        parent[-1].tail = (parent[-1].tail or '') + el.text
                    else:
                        parent.text = (parent.text or '') + el.text

                if len(el):  # then unpack all its children
                    parent.extend(el[:])

                if el.tail:  # then attach tail to previous element
                    if len(parent):  # prev is not parent
                        parent[-1].tail = (parent[-1].tail or '') + el.tail
                    else:
                        parent.text = (parent.text or '') + el.tail

        return None


# alternatively, use cmarkgfm? The maintainer of py-gfm (impl'd over
# python-markdown) ultimately gave up, if apparently mostly due to pymarkdown's
# tendency to break its API all the time
dfm_renderer = markdown.Markdown(
    extensions=[DfmExtension()],
    output_format='html5',
)
