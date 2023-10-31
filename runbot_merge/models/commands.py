import enum
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from operator import contains
from typing import Callable, List, Optional, Union


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


class Reject:
    def __str__(self) -> str:
        return 'review-'


class MergeMethod(enum.Enum):
    SQUASH = 'squash'
    REBASE_FF = 'rebase-ff'
    REBASE_MERGE = 'rebase-merge'
    MERGE = 'merge'

    def __str__(self) -> str:
        return self.value


class Retry:
    def __str__(self) -> str:
        return 'retry'


class Check:
    def __str__(self) -> str:
        return 'check'


@dataclass
class Override:
    statuses: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"override={','.join(self.statuses)}"


@dataclass
class Delegate:
    users: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.users:
            return 'delegate+'
        return f"delegate={','.join(self.users)}"


class Priority(enum.Enum):
    DEFAULT = enum.auto()
    PRIORITY = enum.auto()
    ALONE = enum.auto()

    def __str__(self) -> str:
        return self.name.lower()


class CancelStaging:
    def __str__(self) -> str:
        return "cancel=staging"


class SkipChecks:
    def __str__(self) -> str:
        return 'skipchecks'


class FW(enum.Enum):
    DEFAULT = enum.auto()
    SKIPCI = enum.auto()
    SKIPMERGE = enum.auto()

    def __str__(self) -> str:
        return f'fw={self.name.lower()}'


@dataclass
class Limit:
    branch: Optional[str]

    def __str__(self) -> str:
        if self.branch is None:
            return 'ignore'
        return f'up to {self.branch}'


class Close:
    def __str__(self) -> str:
        return 'close'


Command = Union[
    Approve,
    CancelStaging,
    Close,
    Check,
    Delegate,
    FW,
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
        return CancelStaging()

    def parse_skipchecks(self) -> SkipChecks:
        return SkipChecks()

    def parse_fw(self) -> FW:
        self.assert_next('=')
        f = next(self.it, "")
        try:
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
            raise CommandError("please provide a branch to forward-port to.")

    def parse_close(self) -> Close:
        return Close()
