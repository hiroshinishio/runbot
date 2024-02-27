"""This is definitely the giantest of fucks as pretty much the entire model was
reworked
"""
import dataclasses
import logging
from collections import defaultdict
from itertools import chain
from typing import TypeVar, Any

from psycopg2.extras import execute_batch, execute_values
from psycopg2.sql import SQL

logger = logging.getLogger("odoo.modules.migration.runbot_merge.15.0.1.12")

def cleanup(cr):
    """There seems to be some *pretty* weird database state having crept
    """
    # Until 2021 (not sure why exactly) a bunch of batches were created with no
    # PRs, some staged and some not.
    logger.info("Delete batches without PRs...")
    cr.execute("""
    DELETE FROM runbot_merge_batch
    WHERE id IN (
        SELECT b.id
        FROM runbot_merge_batch b
        LEFT JOIN runbot_merge_batch_runbot_merge_pull_requests_rel r ON (b.id = r.runbot_merge_batch_id)
        WHERE r.runbot_merge_batch_id IS NULL
    )
    """)
    # some of the batches above were the only ones of their stagings
    logger.info("Delete stagings without batches...")
    cr.execute("""
    DELETE FROM runbot_merge_stagings
    WHERE id IN (
        SELECT s.id
        FROM runbot_merge_stagings s
        LEFT JOIN runbot_merge_batch b ON (s.id = b.staging_id)
        WHERE b.id IS NULL
    )
    """)

    # check PRs whose source has a source
    cr.execute("""
    SELECT
        p.id AS id,
        s.id AS source_id,
        r.name || '#' || p.number AS pr,
        pr.name || '#' || pp.number AS parent,
        sr.name || '#' || s.number AS source

    FROM runbot_merge_pull_requests p
    JOIN runbot_merge_repository r ON (r.id = p.repository)

    JOIN runbot_merge_pull_requests pp ON (pp.id = p.source_id)
    JOIN runbot_merge_repository pr ON (pr.id = pp.repository)

    JOIN runbot_merge_pull_requests s ON (s.id = pp.source_id)
    JOIN runbot_merge_repository sr ON (sr.id = s.repository)
    ORDER BY p.id;
    """)
    for pid, ssid, _, _, _ in cr.fetchall():
        cr.execute("UPDATE runbot_merge_pull_requests SET source_id = %s WHERE id = %s", [ssid, pid])

def hlink(url):
    """A terminal hlink starts with OSC8;{params};{link}ST and ends with the
    sequence with no params or link
    """
    return f'\x9d8;;{url}\x9c'

def link(label, url):
    return f"{hlink(url)}{label}{hlink('')}"

def migrate(cr, version):
    cr.execute("select 1 from forwardport_batches")
    assert not cr.rowcount, f"can't migrate the mergebot with enqueued forward ports (found {cr.rowcount})"
    # avoid SQL taking absolutely ungodly amounts of time
    cr.execute("SET statement_timeout = '60s'")
    # will be recreated & computed on the fly
    cr.execute("ALTER TABLE runbot_merge_batch DROP COLUMN target")

    cleanup(cr)

    cr.execute("""
    SELECT
        source_name,
        array_agg(json_build_array(gs.target, gs.prs) order by gs.seq desc)
    FROM (
        SELECT
            rr.name || '#' || source.number as source_name,
            t.sequence as seq,
            t.name as target,
            array_agg(json_build_array(r.name || '#' || p.number, p.state)) as prs

        FROM runbot_merge_pull_requests p
        JOIN runbot_merge_repository r ON (r.id = p.repository)
        JOIN runbot_merge_branch t ON (t.id = p.target)

        JOIN runbot_merge_pull_requests source ON (source.id = p.source_id)
        JOIN runbot_merge_repository rr ON (rr.id = source.repository)

        GROUP BY source.id, rr.id, t.id
        HAVING count(*) FILTER (WHERE p.state = 'merged') > 1
    ) gs
    GROUP BY source_name
    """)
    if cr.rowcount:
        msg = "Found inconsistent batches, which will confuse later chaining\n\n"
        for source, per_target in cr._obj:
            msg += f"source {source}\n"
            for target, prs in per_target:
                msg += "\t{} {}\n".format(
                    target,
                    ", ".join(f'{p} ({s})' for p, s in prs),
                )
        raise Exception(msg)

    logger.info("add batch columns...")
    cr.execute("""
    CREATE TYPE runbot_merge_batch_priority
        AS ENUM ('default', 'priority', 'alone');

    ALTER TABLE runbot_merge_batch
        -- backfilled from staging
        ADD COLUMN merge_date timestamp,
        -- backfilled from PRs
        ADD COLUMN priority runbot_merge_batch_priority NOT NULL DEFAULT 'default',
        ADD COLUMN skipchecks boolean NOT NULL DEFAULT false,
        ADD COLUMN cancel_staging boolean NOT NULL DEFAULT false,
        ADD COLUMN fw_policy varchar NOT NULL DEFAULT 'default'
    ;
    """)
    # batches not linked to stagings are likely to be useless
    logger.info("add batch/staging join table...")
    cr.execute("""
    CREATE TABLE runbot_merge_staging_batch (
        id serial PRIMARY KEY,
        runbot_merge_batch_id integer NOT NULL REFERENCES runbot_merge_batch(id) ON DELETE CASCADE,
        runbot_merge_stagings_id integer NOT NULL REFERENCES runbot_merge_stagings(id) ON DELETE CASCADE
    );
    CREATE UNIQUE INDEX runbot_merge_staging_batch_idx ON runbot_merge_staging_batch
        (runbot_merge_stagings_id, runbot_merge_batch_id);
    CREATE INDEX runbot_merge_staging_batch_rev ON runbot_merge_staging_batch
        (runbot_merge_batch_id) INCLUDE (runbot_merge_stagings_id);
    """)
    # old 'bot creates a new batch at staging time, associated with that
    # specific staging, the way to recoup them (to the best of our ability) is
    # to assume a new style batch is a set of PRs, so if we group batches by prs
    # we get more or less the set of relevant batches / stagings
    logger.info("collect batches...")
    clusters, to_batch = collate_real_batches(cr)

    logger.info("collate batches...")
    to_delete = []
    batch_staging_links = []
    to_rejoin = []
    for cluster in clusters.clusters:
        first = cluster.merged_batch or min(cluster.batches)
        to_delete.extend(cluster.batches - {first})
        # link all the PRs back to that batch
        to_rejoin.append((first, list(cluster.prs)))
        # link `first` to `staging`, ordering insertions by `batch` in order
        # to conserve batching order
        batch_staging_links.extend(
            (batch, first, staging)
            for batch, staging in cluster.stagings
        )

    logger.info("link batches to stagings...")
    # sort (unique_batch, staging) by initial batch so that we create the new
    # bits in the correct order hopefully
    batch_staging_links.sort()
    execute_values(
        cr._obj,
        "INSERT INTO runbot_merge_staging_batch (runbot_merge_batch_id, runbot_merge_stagings_id) VALUES %s",
        ((b, s) for _, b, s in batch_staging_links),
        page_size=1000,
    )

    logger.info("detach PRs from \"active\" batches...")
    # there are non-deactivated batches floating around, which are not linked
    # to stagings, they seem linked to updates (forward-ported PRs getting
    # updated), but not exclusively
    cr.execute("UPDATE runbot_merge_pull_requests SET batch_id = NULL WHERE batch_id IS NOT NULL")
    # drop constraint because pg checks it even though we've set all the active batches to null
    cr.execute("ALTER TABLE runbot_merge_pull_requests DROP CONSTRAINT runbot_merge_pull_requests_batch_id_fkey")

    while to_delete:
        ds, to_delete = to_delete[:10000], to_delete[10000:]
        logger.info("delete %d leftover batches", len(ds))
        cr.execute("DELETE FROM runbot_merge_batch WHERE id = any(%s)", [ds])

    logger.info("delete staging column...")
    cr.execute("ALTER TABLE runbot_merge_batch DROP COLUMN staging_id;")

    logger.info("relink PRs...")
    cr.execute("DROP TABLE runbot_merge_batch_runbot_merge_pull_requests_rel")
    execute_batch(
        cr._obj,
        "UPDATE runbot_merge_pull_requests SET batch_id = %s WHERE id = any(%s)",
        to_rejoin,
        page_size=1000,
    )

    # at this point all the surviving batches should have associated PRs
    cr.execute("""
       SELECT b.id
         FROM runbot_merge_batch b
    LEFT JOIN runbot_merge_pull_requests p ON p.batch_id = b.id
        WHERE p IS NULL;
    """)
    if cr.rowcount:
        logger.error(
            "All batches should have at least one PR, found %d without",
            cr.rowcount,
        )

    # FIXME: fixup PRs marked as merged which don't actually have a batch / staging?

    # the relinked batches are those from stagings, but that means merged PRs
    # (or at least PRs we tried to merge), we also need batches for non-closed
    # non-merged PRs
    logger.info("collect unbatched PRs PRs...")
    cr.execute("""
    SELECT
        CASE
            -- FIXME: should closed PRs w/o a batch be split out or matched with
            --        one another?
            WHEN label SIMILAR TO '%%:patch-[[:digit:]]+'
                THEN id::text
            ELSE label
        END as label_but_not,
        array_agg(id),
        array_agg(distinct target)
    FROM runbot_merge_pull_requests
    WHERE batch_id IS NULL AND id != all(%s)
    GROUP BY label_but_not
    """, [[pid for b in to_batch for pid in b]])
    for _label, ids, targets in cr._obj:
        # a few batches are nonsensical e.g. multiple PRs on different
        # targets from th same branch or mix of master upgrade and stable
        # branch community, split them out
        if len(targets) > 1:
            to_batch.extend([id] for id in ids)
        else:
            to_batch.append(ids)

    logger.info("create %d new batches for unbatched prs...", len(to_batch))
    cr.execute(
        SQL("INSERT INTO runbot_merge_batch VALUES {} RETURNING id").format(
            SQL(", ").join([SQL("(DEFAULT)")]*len(to_batch))))
    logger.info("link unbatched PRs to batches...")
    execute_batch(
        cr._obj,
        "UPDATE runbot_merge_pull_requests SET batch_id = %s WHERE id = any(%s)",
        [(batch_id, ids) for ids, [batch_id] in zip(to_batch, cr.fetchall())],
        page_size=1000,
    )

    cr.execute("SELECT state, count(*) FROM runbot_merge_pull_requests WHERE batch_id IS NULL GROUP BY state")
    if cr.rowcount:
        prs = cr.fetchall()
        logger.error(
            "Found %d PRs without a batch:%s",
            sum(c for _, c in prs),
            "".join(
                f"\n\t- {c} {p!r} PRs"
                for p, c in prs
            ),
        )

    # FIXME: leverage WEIRD_SEQUENCES

    logger.info("move pr data to batches...")
    cr.execute("""
    UPDATE runbot_merge_batch b
    SET merge_date = v.merge_date,
        priority = v.p::varchar::runbot_merge_batch_priority,
        skipchecks = v.skipchecks,
        cancel_staging = v.cancel_staging,
        fw_policy = case when v.skipci
            THEN 'skipci'
            ELSE 'default'
        END
    FROM (
        SELECT
            batch_id as id,
            max(priority) as p,
            min(merge_date) as merge_date,
            -- added to PRs in 1.11 so can be aggregated & copied over
            bool_or(skipchecks) as skipchecks,
            bool_or(cancel_staging) as cancel_staging,
            bool_or(fw_policy = 'skipci') as skipci
        FROM runbot_merge_pull_requests
        GROUP BY batch_id
    ) v
    WHERE b.id = v.id
    """)

    logger.info("restore batch constraint...")
    cr.execute("""
    ALTER TABLE runbot_merge_pull_requests
        ADD CONSTRAINT runbot_merge_pull_requests_batch_id_fkey
        FOREIGN KEY (batch_id)
        REFERENCES runbot_merge_batch (id)
    """)

    # remove xid for x_prs (not sure why it exists)
    cr.execute("""
    DELETE FROM ir_model_data
       WHERE module = 'forwardport'
         AND name = 'field_forwardport_batches__x_prs'
    """)
    # update (x_)prs to match the updated field type(s)
    cr.execute("""
    UPDATE ir_model_fields
       SET ttype = 'one2many',
           relation = 'runbot_merge.pull_requests',
           relation_field = 'batch_id'
     WHERE model_id = 445 AND name = 'prs';

    UPDATE ir_model_fields
       SET ttype = 'one2many'
     WHERE model_id = 448 AND name = 'x_prs';
    """)

    logger.info("generate batch parenting...")
    cr.execute("SELECT id, project_id, name FROM runbot_merge_branch ORDER BY project_id, sequence, name")
    # branch_id -> str
    branch_names = {}
    # branch_id -> project_id
    projects = {}
    # project_id -> list[branch_id]
    branches_for_project = {}
    for bid, pid, name in cr._obj:
        branch_names[bid] = name
        projects[bid] = pid
        branches_for_project.setdefault(pid, []).append(bid)
    cr.execute("""
    SELECT batch_id,
           array_agg(distinct target),
           array_agg(json_build_object(
               'id', p.id,
               'name', r.name || '#' || number,
               'repo', r.name,
               'number', number,
               'state', p.state,
               'source', source_id
           ))
    FROM runbot_merge_pull_requests p
    JOIN runbot_merge_repository r ON (r.id = p.repository)
    GROUP BY batch_id
    """)
    todos = []
    descendants = defaultdict(list)
    targets = {}
    batches = {}
    batch_prs = {}
    for batch, target_ids, prs in cr._obj:
        assert len(target_ids) == 1, \
            "Found batch with multiple targets {tnames} {prs}".format(
                tnames=', '.join(branch_names[id] for id in target_ids),
                prs=prs,
            )

        todos.append((batch, target_ids[0], prs))
        batch_prs[batch] = prs
        for pr in prs:
            pr['link'] = link(pr['name'], "https://mergebot.odoo.com/{repo}/pull/{number}".format_map(pr))

            targets[pr['id']] = target_ids[0]
            batches[pr['id']] = batch
            batches[pr['name']] = batch
            if pr['source']:
                descendants[pr['source']].append(pr['id'])
            else:
                # put source PRs as their own descendants otherwise the linkage
                # fails when trying to find the top-most parent
                descendants[pr['id']].append(pr['id'])
    assert None not in descendants

    for prs in chain(
        KNOWN_BATCHES,
        chain.from_iterable(WEIRD_SEQUENCES),
    ):
        batch_of_prs = {batches[f'odoo/{p}'] for p in prs}
        assert len(batch_of_prs) == 1,\
            "assumed {prs} were the same batch, got {batch_of_prs}".format(
                prs=', '.join(prs),
                batch_of_prs='; '.join(
                    '{} => {}'.format(p, batches[f'odoo/{p}'])
                    for p in prs
                )
            )

        prs_of_batch = {pr['name'].removeprefix('odoo/') for pr in batch_prs[batch_of_prs.pop()]}
        assert set(prs) == prs_of_batch,\
            "assumed batch would contain {prs}, got {prs_of_batch}".format(
                prs=', '.join(prs),
                prs_of_batch=', '.join(prs_of_batch),
            )

    parenting = []
    for batch, target, prs in todos:
        sources = [p['source'] for p in prs if p['source']]
        # can't have parent batch without source PRs
        if not sources:
            continue

        pid = projects[target]
        branches = branches_for_project[pid]

        # we need all the preceding targets in order to jump over disabled branches
        previous_targets = branches[branches.index(target) + 1:]
        if not previous_targets:
            continue

        for previous_target in previous_targets:
            # from each source, find the descendant targeting the earlier target,
            # then get the batch of these PRs
            parents = {
                batches[descendant]
                for source in sources
                for descendant in descendants[source]
                if targets[descendant] == previous_target
            }
            if parents:
                break
        else:
            continue

        if len(parents) == 2:
            parents1, parents2 = [batch_prs[parent] for parent in parents]
            # if all of one parent are merged and all of the other are not, take the merged side
            if all(p['state'] == 'merged' for p in parents1) and all(p['state'] != 'merged' for p in parents2):
                parents = [list(parents)[0]]
            elif all(p['state'] != 'merged' for p in parents1) and all(p['state'] == 'merged' for p in parents2):
                parents = [list(parents)[1]]
            elif len(parents1) == 1 and len(parents2) == 1 and len(prs) == 1:
                # if one of the candidates is older than the current PR
                # (lower id) and the other one younger, assume the first one is
                # correct
                p = min(parents, key=lambda p: batch_prs[p][0]['id'])
                low = batch_prs[p]
                high = batch_prs[max(parents, key=lambda p: batch_prs[p][0]['id'])]
                if low[0]['id'] < prs[0]['id'] < high[0]['id']:
                    parents = [p]

        if real_parents := SAAS_135_INSERTION_CONFUSION.get(tuple(sorted(parents))):
            parents = real_parents

        assert len(parents) == 1,\
            ("Found multiple candidates for batch {batch} ({prs})"
             " with target {target} (previous={previous_target})\n\t{parents}".format(
                parents="\n\t".join(
                    "{} ({})".format(
                        parent,
                        ", ".join(
                            f"{p['link']} ({p['state']}, {branch_names[targets[p['id']]]})"
                            for p in batch_prs[parent]
                        )
                    )
                    for parent in parents
                ),
                batch=batch,
                target=branch_names[target],
                previous_target=branch_names[previous_target],
                prs=', '.join(map("{link} ({state})".format_map, prs)),
            ))
        parenting.append((parents.pop(), batch))

    logger.info("set batch parenting...")
    # add column down here otherwise the FK constraint has to be verified for
    # each batch we try to delete and that is horrendously slow, deferring the
    # constraints is not awesome because we need to check it at the first DDL
    # and that's still way slower than feels necessary
    cr.execute("""
    ALTER TABLE runbot_merge_batch
        ADD COLUMN parent_id integer
        REFERENCES runbot_merge_batch(id)
    """)
    execute_batch(
        cr._obj,
        "UPDATE runbot_merge_batch SET parent_id = %s WHERE id = %s",
        parenting,
        page_size=1000,
    )

@dataclasses.dataclass(slots=True, kw_only=True)
class Cluster:
    merged_batch: int | None = None
    prs: set[int] = dataclasses.field(default_factory=set)
    batches: set[int] = dataclasses.field(default_factory=set)
    stagings: set[tuple[int, int]] = dataclasses.field(default_factory=set)
    "set of original (batch, staging) pairs"

@dataclasses.dataclass
class Clusters:
    clusters: list[Cluster] = dataclasses.field(default_factory=list)
    by_batch: dict[int, Cluster] = dataclasses.field(default_factory=dict)
    by_pr: dict[int, Cluster] = dataclasses.field(default_factory=dict)

@dataclasses.dataclass(slots=True, kw_only=True)
class Batch:
    staging: int | None = None
    merged: bool = False
    prs: set[int] = dataclasses.field(default_factory=set)

T = TypeVar('T')
def insert(s: set[T], v: T) -> bool:
    """Inserts v in s if not in, and returns whether an insertion was needed.
    """
    if v in s:
        return False
    else:
        s.add(v)
        return True
def collate_real_batches(cr: Any) -> tuple[Clusters, list[list[int]]]:
    cr.execute('''
    SELECT
        st.id as staging,
        st.state as staging_state,
        b.id as batch_id,
        p.id as pr_id
    FROM runbot_merge_batch_runbot_merge_pull_requests_rel br
    JOIN runbot_merge_batch b ON (b.id = br.runbot_merge_batch_id)
    JOIN runbot_merge_pull_requests as p ON (p.id = br.runbot_merge_pull_requests_id)
    LEFT JOIN runbot_merge_stagings st ON (st.id = b.staging_id)
    ''')
    batch_map: dict[int, Batch] = {}
    pr_to_batches = defaultdict(set)
    for staging_id, staging_state, batch_id, pr_id in cr.fetchall():
        pr_to_batches[pr_id].add(batch_id)

        if batch := batch_map.get(batch_id):
            batch.prs.add(pr_id)
        else:
            batch_map[batch_id] = Batch(
                staging=staging_id,
                merged=staging_state == 'success',
                prs={pr_id},
            )

    # maps a PR name to its id
    cr.execute("""
    SELECT r.name || '#' || p.number, p.id
      FROM runbot_merge_pull_requests p
      JOIN runbot_merge_repository r ON (r.id = p.repository)
     WHERE r.name || '#' || p.number = any(%s)
    """, [[f'odoo/{p}' for seq in WEIRD_SEQUENCES for b in seq if len(b) > 1 for p in b]])
    prmap: dict[str, int] = dict(cr._obj)
    to_batch = []
    # for each WEIRD_SEQUENCES batch, we need to merge their batches if any,
    # and create them otherwise
    for batch in (b for seq in WEIRD_SEQUENCES for b in seq if len(b) > 1):
        ids = [prmap[f'odoo/{n}'] for n in batch]
        batches = {b for pid in ids for b in pr_to_batches[pid]}
        if batches:
            for pid in ids:
                pr_to_batches[pid].update(batches)
            for bid in batches:
                batch_map[bid].prs.update(ids)
        else:
            # need to create a new batch
            to_batch.append(ids)

    clusters = Clusters()
    # we can start from either the PR or the batch side to reconstruct a cluster
    for pr_id in pr_to_batches:
        if pr_id in clusters.by_pr:
            continue

        to_visit = [pr_id]
        prs: set[int] = set()
        merged_batch = None
        batches: set[int] = set()
        stagings: set[tuple[int, int]] = set()
        while to_visit:
            pr_id = to_visit.pop()
            if not insert(prs, pr_id):
                continue

            for batch_id in pr_to_batches[pr_id]:
                if not insert(batches, batch_id):
                    continue

                b = batch_map[batch_id]
                if s := b.staging:
                    stagings.add((batch_id, s))
                if b.merged:
                    merged_batch = batch_id
                to_visit.extend(b.prs - prs)

        c = Cluster(merged_batch=merged_batch, prs=prs, batches=batches, stagings=stagings)
        clusters.clusters.append(c)
        clusters.by_batch.update((batch_id, c) for batch_id in c.batches)
        clusters.by_pr.update((pr_id, c) for pr_id in c.prs)

    return clusters, to_batch

# at the creation of saas 13.5, the forwardbot clearly got very confused and
# somehow did not correctly link the PRs it reinserted together, leading to
# some of them being merged separately, leading the batch parenting linker thing
# to be extremely confused
SAAS_135_INSERTION_CONFUSION = {
    (48200, 48237): [48237],
    (48353, 48388): [48353],
    (48571, 48602): [48602],
    (73614, 73841): [73614],
}

KNOWN_BATCHES = [
    # both closed, same source (should be trivial)
    ["odoo#151827", "enterprise#55453"],
    ["odoo#66743", "enterprise#16631"],

    # both closed but different sources
    ["odoo#57659", "enterprise#13204"],
    ["odoo#57752", "enterprise#13238"],
    ["odoo#94152", "enterprise#28664"],
    ["odoo#114059", "enterprise#37690"],
    ["odoo#152904", "enterprise#55975"],

    # one closed the other not, different sources (so a PR was added in the
    # middle of a forward port then its descendant was closed evn though the
    # other repo / sequence kept on keeping)
    ["odoo#113422", "enterprise#37429"],
    ["odoo#151992", "enterprise#55501"],
    ["odoo#159211", "enterprise#59407"],

    # closed without a sibling but their source had a sibling
    ["odoo#67727"], # enterprise closed at enterprise#16631
    ["odoo#70828"], # enterprise closed at enterprise#17901
    ["odoo#132817"], # enterprise closed at enterprise#44656
    ["odoo#137855"], # enterprise closed at enterprise#48092
    ["enterprise#49430"], # odoo closed at odoo#139515

    ["odoo#109811", "enterprise#35966"],
    ["odoo#110311", "enterprise#35983"],
    ["odoo#110576"],
]

# This is next level weird compared to the previous so it gets extra care:
# these are sequences with multiple points of divergence or grafting
WEIRD_SEQUENCES = [
    [
        ["odoo#40466"],
        ["odoo#40607"],
        ["odoo#40613", "odoo#41106"],
        ["odoo#40615", "odoo#41112"],
        ["odoo#40627", "odoo#41116", "odoo#41163"],
        ["odoo#40638", "odoo#41119", "odoo#41165"],
    ],
    [
        ["odoo#46405"],
        ["odoo#46698"],
        ["odoo#46820"],
        ["odoo#46974"],
        ["odoo#47273"],
        ["odoo#47345",               "enterprise#9259"],
        ["odoo#47349", "odoo#47724", "enterprise#9274"],
    ],
    [
        ["odoo#47923"],
        ["odoo#47986"],
        ["odoo#47991", "odoo#48010"],
        ["odoo#47996", "odoo#48015", "odoo#48016"],
        ["odoo#48003"],
    ],
    [
        ["enterprise#9996"],
        ["enterprise#10062", "odoo#49828"],
        ["enterprise#10065", "odoo#49852", "enterprise#10076"],
        ["enterprise#10173", "odoo#50087"],
        ["enterprise#10179", "odoo#50104"],
        ["enterprise#10181", "odoo#50110"],
    ],
    [
        ["enterprise#16357"],
        ["enterprise#16371"],
        ["enterprise#16375", "enterprise#16381"],
        ["enterprise#16378", "enterprise#16385"],
        ["enterprise#16379", "enterprise#16390"],
    ],
    [
        ["odoo#55112"],
        ["odoo#55120"],
        ["odoo#55123", "odoo#55159"],
        ["odoo#55128", "odoo#55169"],
        ["odoo#55135", "odoo#55171"],
        ["odoo#55140", "odoo#55172"],
    ],
    [
        ["odoo#56254", "enterprise#12558"],
        ["odoo#56294", "enterprise#12564"],
        ["odoo#56300", "enterprise#12566"],
        ["odoo#56340", "enterprise#12589", "enterprise#12604"],
        ["odoo#56391",                     "enterprise#12608"],
    ],
    [
        ["enterprise#12565", "odoo#56299"],
        ["enterprise#12572", "odoo#56309", "odoo#56494"],
        ["enterprise#12660",               "odoo#56518"],
        ["enterprise#12688",               "odoo#56581"],
        ["enterprise#12691"],
    ],
    [
        ["odoo#64706"],
        ["odoo#65275"],
        ["odoo#65279", "odoo#65405"],
        ["odoo#65489", "odoo#65491"],
    ],
    [
        ["odoo#66176"],
        ["odoo#66188"],
        ["odoo#66191"],
        ["odoo#66194", "odoo#66226"],
        ["odoo#66200", "odoo#66229", "odoo#66277"],
        ["odoo#66204", "odoo#66232", "odoo#66283"],
        ["odoo#66208", "odoo#66234", "odoo#66285", "odoo#66303"],
    ],
    [
        ["enterprise#22089", "odoo#79348"],
        ["enterprise#26736", "odoo#90050"],
        ["enterprise#31822", "odoo#101218", "odoo#106002"],
        ["enterprise#36014",                "odoo#110369", "odoo#113892"],
        ["enterprise#37690",                               "odoo#114059"],
    ],
]
