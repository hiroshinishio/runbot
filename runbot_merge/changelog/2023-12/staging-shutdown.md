ADD: stagings can now be disabled on a per-project basis

Currently stopping stagings requires stopping the staging cron(s), which causes
several issues:

- the staging cron runs very often, so it can be difficult to find a window to
  deactivate it (as the cron runner acquires an exclusive lock on the cron)
- the staging cron is global, so it does not disable staging only on the
  problematic project (to say nothing of branch) but on all of them

The latter is not currently a huge issue as only one of the mergebot-tracked
projects is ultra active (spreadsheet activity is on the order of a few 
single-PR stagings a day), but the former is really annoying when trying to
stop runaway broken stagings.
