import enum
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from operator import contains
from typing import Callable, List, Optional, Union, Tuple


def tokenize(line: str) -> Iterator[str]:
    cur = ''
    for c in line:
        if c == '-' and not cur:
            yield '-'
        elif c in ' \t+=,':
            if cur:
                yield cur
            cur = ''
            if not c.isspace():
                yield c
        else:
            cur += c

    if cur:
        yield cur


def normalize(it: Iterator[str]) -> Iterator[str]:
    """Converts shorthand tokens to expanded version
    """
    for t in it:
        match t:
            case 'r':
                yield 'review'
            case 'r-':
                yield 'review'
                yield '-'
            case _:
                yield t


@dataclass
class Peekable(Iterator[str]):
    it: Iterator[str]
    memo: Optional[str] = None

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        if self.memo is not None:
            v, self.memo = self.memo, None
            return v
        return next(self.it)

    def peek(self) -> Optional[str]:
        if self.memo is None:
            self.memo = next(self.it, None)
        return self.memo


class CommandError(Exception):
    pass


class Approve:
    def __init__(self, ids: Optional[List[int]] = None) -> None:
        self.ids = ids

    def __str__(self) -> str:
        if self.ids is not None:
            ids = ','.join(map(str, self.ids))
            return f"r={ids}"
        return 'review+'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "r(eview)+", "approves the PR, if it's a forwardport also approves all non-detached parents"
        yield "r(eview)=<number>", "only approves the specified parents"

class Reject:
    def __str__(self) -> str:
        return 'review-'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "r(eview)-", "removes approval of a previously approved PR, if the PR is staged the staging will be cancelled"

class MergeMethod(enum.Enum):
    SQUASH = 'squash'
    REBASE_FF = 'rebase-ff'
    REBASE_MERGE = 'rebase-merge'
    MERGE = 'merge'

    def __str__(self) -> str:
        return self.value

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield str(cls.MERGE), "integrate the PR with a simple merge commit, using the PR description as message"
        yield str(cls.REBASE_MERGE), "rebases the PR on top of the target branch the integrates with a merge commit, using the PR description as message"
        yield str(cls.REBASE_FF), "rebases the PR on top of the target branch, then fast-forwards"
        yield str(cls.SQUASH), "squashes the PR as a single commit on the target branch, using the PR description as message"


class Retry:
    def __str__(self) -> str:
        return 'retry'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "retry", 're-tries staging a PR in the "error" state'


class Check:
    def __str__(self) -> str:
        return 'check'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "check", "fetches or refreshes PR metadata, resets mergebot state"


@dataclass
class Override:
    statuses: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"override={','.join(self.statuses)}"

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "override=<...>", "marks overridable statuses as successful"


@dataclass
class Delegate:
    users: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.users:
            return 'delegate+'
        return f"delegate={','.join(self.users)}"

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "delegate+", "grants approval rights to the PR author"
        yield "delegate=<...>", "grants approval rights on this PR to the specified github users"


class Priority(enum.Enum):
    DEFAULT = enum.auto()
    PRIORITY = enum.auto()
    ALONE = enum.auto()

    def __str__(self) -> str:
        return self.name.lower()

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield str(cls.DEFAULT), "stages the PR normally"
        yield str(cls.PRIORITY), "tries to stage this PR first, then adds `default` PRs if the staging has room"
        yield str(cls.ALONE), "stages this PR only with other PRs of the same priority"


class CancelStaging:
    def __str__(self) -> str:
        return "cancel=staging"

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "cancel=staging", "automatically cancels the current staging when this PR becomes ready"


class SkipChecks:
    def __str__(self) -> str:
        return 'skipchecks'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "skipchecks", "bypasses both statuses and review"


class FW(enum.Enum):
    DEFAULT = enum.auto()
    NO = enum.auto()
    SKIPCI = enum.auto()
    SKIPMERGE = enum.auto()

    def __str__(self) -> str:
        return f'fw={self.name.lower()}'

    @classmethod
    def help(cls, is_reviewer: bool) -> Iterator[Tuple[str, str]]:
        yield str(cls.NO), "does not forward-port this PR"
        if is_reviewer:
            yield str(cls.DEFAULT), "forward-ports this PR normally"
            yield str(cls.SKIPCI), "does not wait for a forward-port's statuses to succeed before creating the next one"


@dataclass
class Limit:
    branch: Optional[str]

    def __str__(self) -> str:
        if self.branch is None:
            return 'ignore'
        return f'up to {self.branch}'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield "up to <branch>", "only ports this PR forward to the specified branch (included)"


class Close:
    def __str__(self) -> str:
        return 'close'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield str(cls()), "closes this forward-port"


class Help:
    def __str__(self) -> str:
        return 'help'

    @classmethod
    def help(cls, _: bool) -> Iterator[Tuple[str, str]]:
        yield str(cls()), "displays this help"


Command = Union[
    Approve,
    CancelStaging,
    Close,
    Check,
    Delegate,
    FW,
    Help,
    Limit,
    MergeMethod,
    Override,
    Priority,
    Reject,
    Retry,
    SkipChecks,
]


class Parser:
    def __init__(self, line: str) -> None:
        self.it = Peekable(normalize(tokenize(line)))

    def __iter__(self) -> Iterator[Command]:
        for token in self.it:
            if token.startswith("NOW"):
                # any number of ! is allowed
                if token.startswith("NOW!"):
                    yield Priority.ALONE
                elif token == "NOW":
                    yield Priority.PRIORITY
                else:
                    raise CommandError(f"unknown command {token!r}")
                yield SkipChecks()
                yield CancelStaging()
                continue

            handler = getattr(type(self), f'parse_{token.replace("-", "_")}', None)
            if handler:
                yield handler(self)
            elif '!' in token:
                raise CommandError("skill issue, noob")
            else:
                raise CommandError(f"unknown command {token!r}")

    def assert_next(self, val: str) -> None:
        if (actual := next(self.it, None)) != val:
            raise CommandError(f"expected {val!r}, got {actual!r}")

    def check_next(self, val: str) -> bool:
        if self.it.peek() == val:
            self.it.memo = None # consume peeked value
            return True
        return False

    def parse_review(self) -> Union[Approve, Reject]:
        t = next(self.it, None)
        if t == '+':
            return Approve()
        if t == '-':
            return Reject()
        if t == '=':
            t = next(self.it, None)
            if not (t and t.isdecimal()):
                raise CommandError(f"expected PR ID to approve, found {t!r}")

            ids = [int(t)]
            while self.check_next(','):
                id = next(self.it, None)
                if id and id.isdecimal():
                    ids.append(int(id))
                else:
                    raise CommandError(f"expected PR ID to approve, found {id!r}")
            return Approve(ids)

        raise CommandError(f"unknown review {t!r}")

    def parse_squash(self) -> MergeMethod:
        return MergeMethod.SQUASH

    def parse_rebase_ff(self) -> MergeMethod:
        return MergeMethod.REBASE_FF

    def parse_rebase_merge(self) -> MergeMethod:
        return MergeMethod.REBASE_MERGE

    def parse_merge(self) -> MergeMethod:
        return MergeMethod.MERGE

    def parse_retry(self) -> Retry:
        return Retry()

    def parse_check(self) -> Check:
        return Check()

    def parse_override(self) -> Override:
        self.assert_next('=')
        ci = [next(self.it)]
        while self.check_next(','):
            ci.append(next(self.it))
        return Override(ci)

    def parse_delegate(self) -> Delegate:
        match next(self.it, None):
            case '+':
                return Delegate()
            case '=':
                delegates = [next(self.it).lstrip('#@')]
                while self.check_next(','):
                    delegates.append(next(self.it).lstrip('#@'))
                return Delegate(delegates)
            case d:
                raise CommandError(f"unknown delegation {d!r}")

    def parse_default(self) -> Priority:
        return Priority.DEFAULT

    def parse_priority(self) -> Priority:
        return Priority.PRIORITY

    def parse_alone(self) -> Priority:
        return Priority.ALONE

    def parse_cancel(self) -> CancelStaging:
        self.assert_next('=')
        self.assert_next('staging')
        return CancelStaging()

    def parse_skipchecks(self) -> SkipChecks:
        return SkipChecks()

    def parse_fw(self) -> FW:
        self.assert_next('=')
        f = next(self.it, "")
        try:
            if f in ('disable', 'disabled'):
                return FW.NO
            return FW[f.upper()]
        except KeyError:
            raise CommandError(f"unknown fw configuration {f or None!r}") from None

    def parse_ignore(self) -> Limit:
        return Limit(None)

    def parse_up(self) -> Limit:
        self.assert_next('to')
        if limit := next(self.it, None):
            return Limit(limit)
        else:
            raise CommandError("please provide a branch to forward-port to")

    def parse_close(self) -> Close:
        return Close()

    def parse_help(self) -> Help:
        return Help()
