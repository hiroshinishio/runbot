import enum
from collections import abc
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Union


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
            case 'p':
                yield 'priority'
            case _:
                yield t


@dataclass
class Peekable(abc.Iterator[str]):
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


def assert_next(it: Iterator[str], val: str):
    if (actual := next(it, None)) != val:
        raise CommandError(f"expected {val!r}, got {actual!r}")


class CommandError(Exception):
    pass


class Approve:
    def __str__(self):
        return 'review+'


class Reject:
    def __str__(self):
        return 'review-'


class MergeMethod(enum.Enum):
    SQUASH = 'squash'
    REBASE_FF = 'rebase-ff'
    REBASE_MERGE = 'rebase-merge'
    MERGE = 'merge'

    def __str__(self):
        return self.value


class Retry:
    def __str__(self):
        return 'retry'


class Check:
    def __str__(self):
        return 'check'


@dataclass
class Override:
    statuses: List[str] = field(default_factory=list)

    def __str__(self):
        return f"override={','.join(self.statuses)}"


@dataclass
class Delegate:
    users: List[str] = field(default_factory=list)

    def __str__(self):
        if not self.users:
            return 'delegate+'
        return f"delegate={','.join(self.users)}"


class Priority(enum.IntEnum):
    NUKE = 0
    PRIORITIZE = 1
    DEFAULT = 2


Command = Union[Approve, Reject, MergeMethod, Retry, Check, Override, Delegate, Priority]


def parse_mergebot(line: str) -> Iterator[Command]:
    it = Peekable(normalize(tokenize(line)))
    for token in it:
        match token:
            case 'review':
                match next(it):
                    case '+':
                        yield Approve()
                    case '-':
                        yield Reject()
                    case t:
                        raise CommandError(f"unknown review {t!r}")
            case 'squash':
                yield MergeMethod.SQUASH
            case 'rebase-ff':
                yield MergeMethod.REBASE_FF
            case 'rebase-merge':
                yield MergeMethod.REBASE_MERGE
            case 'merge':
                yield MergeMethod.MERGE
            case 'retry':
                yield Retry()
            case 'check':
                yield Check()
            case 'override':
                assert_next(it, '=')
                ci = [next(it)]
                while it.peek() == ',':
                    next(it)
                    ci.append(next(it))
                yield Override(ci)
            case 'delegate':
                match next(it):
                    case '+':
                        yield Delegate()
                    case '=':
                        delegates = [next(it).lstrip('#@')]
                        while it.peek() == ',':
                            next(it)
                            delegates.append(next(it).lstrip('#@'))
                        yield Delegate(delegates)
                    case d:
                        raise CommandError(f"unknown delegation {d!r}")
            case 'priority':
                assert_next(it, '=')
                yield Priority(int(next(it)))
            case c:
                raise CommandError(f"unknown command {c!r}")


class FWApprove:
    def __str__(self):
        return 'review+'


@dataclass
class CI:
    run: bool
    def __str__(self):
        return 'ci' if self.run else 'skipci'


@dataclass
class Limit:
    branch: Optional[str]

    def __str__(self):
        if self.branch is None:
            return 'ignore'
        return f'up to {self.branch}'


class Close:
    def __str__(self):
        return 'close'


FWCommand = Union[FWApprove, CI, Limit, Close]


def parse_forwardbot(line: str) -> Iterator[FWCommand]:
    it = Peekable(normalize(tokenize(line)))
    for token in it:
        match token:
            case 'review':
                match next(it):
                    case '+':
                        yield FWApprove()
                    case t:
                        raise CommandError(f"unknown review {t!r}", True)
            case 'ci':
                yield CI(True)
            case 'skipci':
                yield CI(False)
            case 'ignore':
                yield Limit(None)
            case 'up':
                assert_next(it, 'to')
                if limit := next(it, None):
                    yield Limit(limit)
                else:
                    raise CommandError("please provide a branch to forward-port to.", True)
            case 'close':
                yield Close()
            case c:
                raise CommandError(f"unknown command {c!r}", True)
