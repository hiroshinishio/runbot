CHG: complete rework of the commands system

# fun is dead: strict commands parsing

Historically the bots would apply whatever looked like a command and ignore the
rest. This led to people sending novels to the bot, then being surprised the bot
found a command in the mess.

The bots now ignore all lines which contain any non-command. Example:

> @robodoo r+ when green darling

Previously, the bot would apply the `r+` and ignore the rest. Now the bot will
ignore everything and reply with

> unknown command "when"

# fwbot is dead

The mergebot (@robodoo) is now responsible for the old fwbot commands:

- close, ignore, up to, ... work as they ever did, just with robodoo
- `robodoo r+` now approves the parents if the current PR a forward port
  - a specific PR can be approved even in forward ports by providing its number
    e.g. `robodoo r=45328` will approve just PR 45328, if that is the PR the
    comment is being posted on or one of its parents
  - the approval of forward ports won't skip over un-approvable PRs anymore
  - the rights of the original author have been restricted slightly: they can
    only approve the direct descendents of merged PRs, so if one of the parents
    has been modified and is not merged yet, the original author can't approve,
    nor can they approve the modified PR, or a conflicting PR which has to get
    fixed (?)

# no more p=<number>

The old priorities command was a tangle of multiple concerns, not all of which
were always desired or applicable. These tangles have been split along their
various axis.

# listing

The new commands are:

- `default`, sets the staging priority back to the default
- `priority`, sets the staging priority to elevated, on staging these PRs are
  staged first, then the `normal` PRs are added
- `alone`, sets the staging priority to high, these PRs are staged before
  considering splits, and only `alone` PRs are staged together even if the batch
  is not full
- `fw=default`, processes forward ports normally
- `fw=skipci`, once the current PR has been merged creates all the forward ports
  without waiting for each to have valid statuses
- `fw=skipmerge`, immediately create all forward ports even if the base pull
  request has not even been merged yet
- `skipchecks`, makes the entire batch (target PR and any linked PR) immediately 
  ready, bypassing statuses and reviews
- `cancel`, cancels the staging on the target branch, if any
