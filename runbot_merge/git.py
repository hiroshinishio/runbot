import dataclasses
import itertools
import logging
import os
import pathlib
import resource
import subprocess
from typing import Optional, TypeVar, Union, Generic, Sequence, Tuple, Dict

from odoo.tools.appdirs import user_cache_dir
from .github import MergeError, PrCommit

_logger = logging.getLogger(__name__)


def source_url(repository, prefix: str) -> str:
    return 'https://{}@github.com/{}'.format(
        repository.project_id[f'{prefix}_token'],
        repository.name,
    )


def get_local(repository, prefix: Optional[str]) -> 'Optional[Repo[subprocess.CompletedProcess]]':
    repos_dir = pathlib.Path(user_cache_dir('mergebot'))
    repos_dir.mkdir(parents=True, exist_ok=True)
    # NB: `repository.name` is `$org/$name` so this will be a subdirectory, probably
    repo_dir = repos_dir / repository.name

    if repo_dir.is_dir():
        return git(repo_dir)
    elif prefix:
        _logger.info("Cloning out %s to %s", repository.name, repo_dir)
        subprocess.run(['git', 'clone', '--bare', source_url(repository, prefix), str(repo_dir)], check=True)
        # bare repos don't have fetch specs by default, and fetching *into*
        # them is a pain in the ass, configure fetch specs so `git fetch`
        # works properly
        repo = git(repo_dir)
        repo.config('--add', 'remote.origin.fetch', '+refs/heads/*:refs/heads/*')
        # negative refspecs require git 2.29
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/tmp.*')
        repo.config('--add', 'remote.origin.fetch', '^refs/heads/staging.*')
        return repo


ALWAYS = ('gc.auto=0', 'maintenance.auto=0')


def _bypass_limits():
    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))


def git(directory: str) -> 'Repo[subprocess.CompletedProcess]':
    return Repo(directory, check=True)


Self = TypeVar("Self", bound="Repo")
T = TypeVar('T', bound=Union[subprocess.CompletedProcess, subprocess.Popen])
class Repo(Generic[T]):
    def __init__(self, directory, **config) -> None:
        self._directory = str(directory)
        config.setdefault('stderr', subprocess.PIPE)
        self._config = config
        self._params = ()
        self._opener = subprocess.run

    def __getattr__(self, name: str) -> 'GitCommand':
        return GitCommand(self, name.replace('_', '-'))

    def _run(self, *args, **kwargs) -> T:
        opts = {**self._config, **kwargs}
        args = ('git', '-C', self._directory)\
            + tuple(itertools.chain.from_iterable(('-c', p) for p in self._params + ALWAYS))\
            + args
        try:
            return self._opener(args, preexec_fn=_bypass_limits, **opts)
        except subprocess.CalledProcessError as e:
            stream = e.stderr if e.stderr else e.stdout
            if stream:
                _logger.error("git call error: %s", stream)
            raise

    def stdout(self, flag: bool = True) -> Self:
        if flag is True:
            return self.with_config(stdout=subprocess.PIPE)
        elif flag is False:
            return self.with_config(stdout=None)
        return self.with_config(stdout=flag)

    def lazy(self) -> 'Repo[subprocess.Popen]':
        r = self.with_config()
        r._config.pop('check', None)
        r._opener = subprocess.Popen
        return r

    def check(self, flag: bool) -> Self:
        return self.with_config(check=flag)

    def with_config(self, **kw) -> Self:
        opts = {**self._config, **kw}
        r = Repo(self._directory, **opts)
        r._opener = self._opener
        r._params = self._params
        return r

    def with_params(self, *args) -> Self:
        r = self.with_config()
        r._params = args
        return r

    def clone(self, to: str, branch: Optional[str] = None) -> 'Repo[subprocess.CompletedProcess]':
        self._run(
            'clone',
            *([] if branch is None else ['-b', branch]),
            self._directory, to,
        )
        return Repo(to)

    def rebase(self, dest: str, commits: Sequence[PrCommit]) -> Tuple[str, Dict[str, str]]:
        """Implements rebase by hand atop plumbing so:

        - we can work without a working copy
        - we can track individual commits (and store the mapping)

        It looks like `--merge-base` is not sufficient for `merge-tree` to
        correctly keep track of history, so it loses contents. Therefore
        implement in two passes as in the github version.
        """
        repo = self.stdout().with_config(text=True, check=False)

        logger = _logger.getChild('rebase')
        logger.debug("rebasing %s on %s (reset=%s, commits=%s)",
                     self._repo, dest, len(commits))
        if not commits:
            raise MergeError("PR has no commits")

        new_trees = []
        parent = dest
        for original in commits:
            if len(original['parents']) != 1:
                raise MergeError(
                    f"commits with multiple parents ({original['sha']}) can not be rebased, "
                    "either fix the branch to remove merges or merge without "
                    "rebasing")

            new_trees.append(check(repo.merge_tree(parent, original['sha'])).stdout.strip())
            parent = check(repo.commit_tree(
                new_trees[-1],
                '-p', parent,
                '-p', original['sha'],
                '-m', f'temp rebase {original["sha"]}',
            )).stdout.strip()

        mapping = {}
        for original, t in zip(commits, new_trees):
            authorship = check(repo.show('--no-patch', '--pretty="%an%n%ae%n%ai%n%cn%n%ce"', original['sha']))
            author_name, author_email, author_date, committer_name, committer_email =\
                authorship.stdout.splitlines()

            c = check(repo.with_config(env={
                **os.environ,
                'GIT_AUTHOR_NAME': author_name,
                'GIT_AUTHOR_EMAIL': author_email,
                'GIT_AUTHOR_DATE': author_date,
                'GIT_COMMITTER_NAME': committer_name,
                'GIT_COMMITTER_EMAIL': committer_email,
                'TZ': 'UTC',
            }).commit_tree(t, p=dest, m=original['commit']['message']))\
                .stdout.strip()

            logger.debug('copied %s to %s (parent: %s)', original['sha'], c, dest)
            dest = mapping[original['sha']] = c

        return dest, mapping

    def merge(self, c1: str, c2: str, msg: str, *, author: Tuple[str, str]) -> str:
        repo = self.stdout().with_config(text=True, check=False)

        t = repo.merge_tree(c1, c2)
        if t.returncode:
            raise MergeError(t.stderr)

        c = repo.with_config(env={
            **os.environ,
            'GIT_AUTHOR_NAME': author[0],
            'GIT_AUTHOR_EMAIL': author[1],
            'TZ': 'UTC',
        }).commit_tree(
            t.stdout.strip(),
            '-p', c1,
            '-p', c2,
            '-m', msg,
        )
        if c.returncode:
            raise MergeError(c.stderr)
        return c.stdout.strip()

def check(p: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    if not p.returncode:
        return p

    _logger.info("rebase failed at %s\nstdout:\n%s\nstderr:\n%s", p.args, p.stdout, p.stderr)
    raise MergeError(p.stderr or 'merge conflict')


@dataclasses.dataclass
class GitCommand(Generic[T]):
    repo: Repo[T]
    name: str

    def __call__(self, *args, **kwargs) -> T:
        return self.repo._run(self.name, *args, *self._to_options(kwargs))

    def _to_options(self, d):
        for k, v in d.items():
            if len(k) == 1:
                yield '-' + k
            else:
                yield '--' + k.replace('_', '-')
            if v not in (None, True):
                assert v is not False
                yield str(v)