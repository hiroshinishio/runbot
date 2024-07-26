# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import collections
import colorsys
import hashlib
import io
import json
import logging
import pathlib
from dataclasses import dataclass
from email.utils import formatdate
from enum import Flag, auto
from functools import cached_property
from itertools import chain, product
from math import ceil
from typing import Tuple, cast, Mapping, Optional, List

import markdown
import markupsafe
import werkzeug.exceptions
import werkzeug.wrappers
from PIL import Image, ImageDraw, ImageFont

from odoo.http import Controller, route, request
from odoo.tools import file_open

HORIZONTAL_PADDING = 20
VERTICAL_PADDING = 5

_logger = logging.getLogger(__name__)

LIMIT = 20
class MergebotDashboard(Controller):
    @route('/runbot_merge', auth="public", type="http", website=True, sitemap=True)
    def dashboard(self):
        projects = request.env['runbot_merge.project'].with_context(active_test=False).sudo().search([])
        stagings = {
            branch: projects.env['runbot_merge.stagings'].search([
                ('target', '=', branch.id)], order='staged_at desc', limit=6)
            for project in projects
            for branch in project.branch_ids
            if branch.active
        }
        prefetch_set = list({
            id
            for stagings in stagings.values()
            for id in stagings.ids
        })
        for st in stagings.values():
            st._prefetch_ids = prefetch_set

        return request.render('runbot_merge.dashboard', {
            'projects': projects,
            'stagings_map': stagings,
        })

    @route('/runbot_merge/<int:branch_id>', auth='public', type='http', website=True, sitemap=False)
    def stagings(self, branch_id, until=None, state=''):
        branch = request.env['runbot_merge.branch'].browse(branch_id).sudo().exists()
        if not branch:
            raise werkzeug.exceptions.NotFound()

        staging_domain = [('target', '=', branch.id)]
        if until:
            staging_domain.append(('staged_at', '<=', until))
        if state:
            staging_domain.append(('state', '=', state))

        stagings = request.env['runbot_merge.stagings'].with_context(active_test=False).sudo().search(staging_domain, order='staged_at desc', limit=LIMIT + 1)

        return request.render('runbot_merge.branch_stagings', {
            'branch': branch,
            'stagings': stagings[:LIMIT],
            'until': until,
            'state': state,
            'next': stagings[-1].staged_at if len(stagings) > LIMIT else None,
        })

    def _entries(self):
        changelog = pathlib.Path(__file__).parent.parent / 'changelog'
        if changelog.is_dir():
            return [
                (d.name, [f.read_text(encoding='utf-8') for f in d.iterdir() if f.is_file()])
                for d in changelog.iterdir()
            ]
        return []

    def entries(self, item_converter):
        entries = collections.OrderedDict()
        for key, items in sorted(self._entries(), reverse=True):
            entries.setdefault(key, []).extend(map(item_converter, items))
        return entries

    @route('/runbot_merge/changelog', auth='public', type='http', website=True, sitemap=True)
    def changelog(self):
        md = markdown.Markdown(extensions=['nl2br'], output_format='html5')
        entries = self.entries(lambda t: markupsafe.Markup(md.convert(t)))
        return request.render('runbot_merge.changelog', {
            'entries': entries,
        })

    @route('/<org>/<repo>/pull/<int(min=1):pr><any("", ".png"):png>', auth='public', type='http', website=True, sitemap=False)
    def pr(self, org, repo, pr, png):
        pr_id = request.env['runbot_merge.pull_requests'].sudo().search([
            ('repository.name', '=', f'{org}/{repo}'),
            ('number', '=', int(pr)),
        ])
        if not pr_id:
            raise werkzeug.exceptions.NotFound()
        if not pr_id.repository.group_id <= request.env.user.groups_id:
            _logger.warning(
                "Access error: %s (%s) tried to access %s but lacks access",
                request.env.user.login,
                request.env.user.name,
                pr_id.display_name,
            )
            raise werkzeug.exceptions.NotFound()

        if png:
            return raster_render(pr_id)

        st = {}
        if pr_id.statuses:
            # normalise `statuses` to map to a dict
            st = {
                k: {'state': v} if isinstance(v, str) else v
                for k, v in json.loads(pr_id.statuses_full).items()
            }
        return request.render('runbot_merge.view_pull_request', {
            'pr': pr_id,
            'merged_head': json.loads(pr_id.commits_map).get(''),
            'statuses': st
        })

def raster_render(pr):
    default_headers = {
        'Content-Type': 'image/png',
        'Last-Modified': formatdate(),
        # - anyone can cache the image, so public
        # - crons run about every minute so that's how long a request is fresh
        # - if the mergebot can't be contacted, allow using the stale response (no must-revalidate)
        # - intermediate caches can recompress the PNG if they want (pillow is not a very good PNG generator)
        # - the response is mutable even during freshness, technically (as there
        #   is no guarantee the freshness window lines up with the cron, plus
        #   some events are not cron-based)
        # - maybe don't allow serving the stale image *while* revalidating?
        # - allow serving a stale image for a day if the server returns 500
        'Cache-Control': 'public, max-age=60, stale-if-error=86400',
    }
    if if_none_match := request.httprequest.headers.get('If-None-Match'):
        # just copy the existing value out if we received any
        default_headers['ETag'] = if_none_match

    # weak validation: check the latest modification date of all objects involved
    project, repos, branches, genealogy = pr.env.ref('runbot_merge.dashboard-pre')\
        ._run_action_code_multi({'pr': pr})

    # last-modified should be in RFC2822 format, which is what
    # email.utils.formatdate does (sadly takes a timestamp but...)
    last_modified = formatdate(max((
        o.write_date
        for o in chain(
            project,
            repos,
            branches,
            genealogy,
            genealogy.all_prs | pr,
        )
    )).timestamp())
    # The (304) response must not contain a body and must include the headers
    # that would have been sent in an equivalent 200 OK response
    headers = {**default_headers, 'Last-Modified': last_modified}
    if request.httprequest.headers.get('If-Modified-Since') == last_modified:
        return werkzeug.wrappers.Response(status=304, headers=headers)

    batches = pr.env.ref('runbot_merge.dashboard-prep')._run_action_code_multi({
        'pr': pr,
        'repos': repos,
        'branches': branches,
        'genealogy': genealogy,
    })

    etag = hashlib.sha256(f"(P){pr.id},{pr.repository.id},{pr.target.id},{pr.batch_id.blocked}".encode())
    # repos and branches should be in a consistent order so can just hash that
    etag.update(''.join(f'(R){r.name}' for r in repos).encode())
    etag.update(''.join(f'(T){b.name},{b.active}' for b in branches).encode())
    # and product of deterministic iterations should be deterministic
    for r, b in product(repos, branches):
        ps = batches[r, b]

        etag.update(f"(B){ps['state']},{ps['detached']},{ps['active']}".encode())
        etag.update(''.join(
            f"(PS){p['label']},{p['closed']},{p['number']},{p['checked']},{p['reviewed']},{p['attached']},{p['pr'].staging_id.id}"
            for p in ps['prs']
        ).encode())

    etag = headers['ETag'] = base64.b32encode(etag.digest()).decode()
    if if_none_match == etag:
        return werkzeug.wrappers.Response(status=304, headers=headers)

    if not pr.batch_id.target:
        im = render_inconsistent_batch(pr.batch_id)
    else:
        im = render_full_table(pr, branches, repos, batches)

    buffer = io.BytesIO()
    im.save(buffer, 'png', optimize=True)
    return werkzeug.wrappers.Response(buffer.getvalue(), headers=headers)

class Decoration(Flag):
    STRIKETHROUGH = auto()

@dataclass(frozen=True)
class Text:
    content: str
    font: ImageFont.FreeTypeFont
    color: Color
    decoration: Decoration = Decoration(0)

    @cached_property
    def width(self) -> int:
        return ceil(self.font.getlength(self.content))

    @property
    def height(self) -> int:
        return sum(self.font.getmetrics())

    def draw(self, image: ImageDraw.ImageDraw, left: int, top: int):
        image.text((left, top), self.content, fill=self.color, font=self.font)
        if Decoration.STRIKETHROUGH in self.decoration:
            x1, _, x2, _ = self.font.getbbox(self.content)
            _, y1, _, y2 = self.font.getbbox('x')
            # put the strikethrough line about 1/3rd down the x (default seems
            # to be a bit above halfway down but that's ugly with numbers which
            # is most of our stuff)
            y = top + y1 + (y2 - y1) / 3
            image.line([(left + x1, y), (left + x2, y)], self.color)

@dataclass(frozen=True)
class Checkbox:
    checked: Optional[bool]
    font: ImageFont.FreeTypeFont
    color: Color
    success: Color
    error: Color

    @cached_property
    def width(self) -> int:
        return ceil(max(
            self.font.getlength(BOX_EMPTY),
            self.font.getlength(CHECK_MARK),
            self.font.getlength(CROSS),
        ))

    @property
    def height(self):
        return sum(self.font.getmetrics())

    def draw(self, image: ImageDraw.ImageDraw, left: int, top: int):
        image.text((left, top+5), BOX_EMPTY, fill=self.color, font=self.font)
        if self.checked is True:
            image.text((left, top+4), CHECK_MARK, fill=self.success, font=self.font)
        elif self.checked is False:
            image.text((left, top+4), CROSS, fill=self.error, font=self.font)

@dataclass(frozen=True)
class Line:
    spans: List[Text | Checkbox | Lines]

    @property
    def width(self) -> int:
        return sum(s.width for s in self.spans)

    @property
    def height(self) -> int:
        return max(s.height for s in self.spans) if self.spans else 0

    def draw(self, image: ImageDraw.ImageDraw, left: int, top: int):
        for span in self.spans:
            span.draw(image, left, top)
            left += span.width

@dataclass(frozen=True)
class Lines:
    lines: List[Line]

    @property
    def width(self) -> int:
        return max(l.width for l in self.lines)

    @property
    def height(self) -> int:
        return sum(l.height for l in self.lines)

    def draw(self, image: ImageDraw.ImageDraw, left: int, top: int):
        for line in self.lines:
            line.draw(image, left, top)
            top += line.height

@dataclass(frozen=True)
class Cell:
    content: Lines | Line | Text
    background: Color = (255, 255, 255)
    attached: bool = True

    @cached_property
    def width(self) -> int:
        return self.content.width + 2 * HORIZONTAL_PADDING

    @cached_property
    def height(self) -> int:
        return self.content.height + 2 * VERTICAL_PADDING


def render_full_table(pr, branches, repos, batches):
    with file_open('web/static/fonts/google/Open_Sans/Open_Sans-Regular.ttf', 'rb') as f:
        font = ImageFont.truetype(f, size=16, layout_engine=0)
        f.seek(0)
        supfont = ImageFont.truetype(f, size=13, layout_engine=0)
    with file_open('web/static/fonts/google/Open_Sans/Open_Sans-Bold.ttf', 'rb') as f:
        bold = ImageFont.truetype(f, size=16, layout_engine=0)
    with file_open('web/static/lib/fontawesome/fonts/fontawesome-webfont.ttf', 'rb') as f:
        icons = ImageFont.truetype(f, size=16, layout_engine=0)

    rowheights = collections.defaultdict(int)
    colwidths = collections.defaultdict(int)
    cells = {}
    for b in chain([None], branches):
        for r in chain([None], repos):
            opacity = 1.0 if b is None or b.active else 0.5
            current_row = b == pr.target
            background = BG['info'] if current_row or r == pr.repository else BG[None]

            if b is None: # first row
                cell = Cell(Text("" if r is None else r.name, bold, TEXT), background)
            elif r is None: # first column
                cell = Cell(Text(b.name, font, blend(TEXT, opacity, over=background)), background)
            elif current_row:
                ps = batches[r, b]
                bgcolor = lighten(BG[ps['state']], by=-0.05) if pr in ps['pr_ids'] else BG[ps['state']]
                background = blend(bgcolor, opacity, over=background)
                foreground = blend((39, 110, 114), opacity, over=background)
                success = blend(SUCCESS, opacity, over=background)
                error = blend(ERROR, opacity, over=background)

                boxes = {
                    False: Checkbox(False, icons, foreground, success, error),
                    True: Checkbox(True, icons, foreground, success, error),
                    None: Checkbox(None, icons, foreground, success, error),
                }
                prs = []
                attached = True
                for p in ps['prs']:
                    pr = p['pr']
                    attached = attached and p['attached']
                    sub = ": staging failed" if pr.error else ""
                    lines = [
                        Line([Text(
                            f"#{p['number']}{sub}",
                            font,
                            foreground,
                            decoration=Decoration.STRIKETHROUGH if p['closed'] else Decoration(0),
                        )]),
                    ]

                    # no need for details if closed or in error
                    if not (p['closed'] or pr.error):
                        if pr.draft:
                            lines.append(Line([boxes[False], Text("is in draft", font, error)]))
                        lines.extend([
                            Line([
                                boxes[bool(pr.squash or pr.merge_method)],
                                Text(
                                    "merge method: {}".format('single' if pr.squash else (pr.merge_method or 'missing')),
                                    font,
                                    foreground if pr.squash or pr.merge_method else error,
                                ),
                            ]),
                            Line([
                                boxes[bool(pr.reviewed_by)],
                                Text(
                                    "Reviewed" if pr.reviewed_by else "Not Reviewed",
                                    font,
                                    foreground if pr.reviewed_by else error,
                                )
                            ]),
                            Line([
                                boxes[pr.batch_id.skipchecks or pr.status == 'success'],
                                Text("CI", font, foreground if pr.batch_id.skipchecks or pr.status == 'success' else error),
                            ]),
                        ])
                        if not pr.batch_id.skipchecks:
                            statuses = json.loads(pr.statuses_full)
                            for ci in pr.repository.status_ids._for_pr(pr):
                                st = (statuses.get(ci.context.strip()) or {'state': 'pending'})['state']
                                color = foreground
                                if st in ('error', 'failure'):
                                    color = error
                                    box = boxes[False]
                                elif st == 'success':
                                    box = boxes[True]
                                else:
                                    box = boxes[None]

                                lines.append(Line([
                                    Text(" - ", font, color),
                                    box,
                                    Text(f"{ci.repo_id.name}: {ci.context}", font, color)
                                ]))
                    prs.append(Lines(lines))
                cell = Cell(Line(prs), background, attached)
            else:
                ps = batches[r, b]
                bgcolor = lighten(BG[ps['state']], by=-0.05) if pr in ps['pr_ids'] else BG[ps['state']]
                background = blend(bgcolor, opacity, over=background)
                foreground = blend((39, 110, 114), opacity, over=background)

                line = []
                attached = True
                for p in ps['prs']:
                    line.append(Text(
                        f"#{p['number']}",
                        font,
                        foreground,
                        decoration=Decoration.STRIKETHROUGH if p['closed'] else Decoration(0),
                    ))
                    attached = attached and p['attached']
                    for attribute in filter(None, [
                        'error' if p['pr'].error else '',
                        '' if p['checked'] else 'missing statuses',
                        '' if p['reviewed'] else 'missing r+',
                        '' if p['attached'] else 'detached',
                        'staged' if p['pr'].staging_id else 'ready' if p['pr']._ready else ''
                    ]):
                        color = SUCCESS if attribute in ('staged', 'ready') else ERROR
                        line.append(Text(f' {attribute}', supfont, blend(color, opacity, over=background)))
                    line.append(Text(" ", font, foreground))
                cell = Cell(Line(line), background, attached)

            cells[r, b] = cell
            rowheights[b] = max(rowheights[b], cell.height)
            colwidths[r] = max(colwidths[r], cell.width)

    im = Image.new("RGB", (sum(colwidths.values()), sum(rowheights.values())), "white")
    # no need to set the font here because every text element has its own
    draw = ImageDraw.Draw(im, 'RGB')
    top = 0
    for b in chain([None], branches):
        left = 0
        for r in chain([None], repos):
            cell = cells[r, b]

            # for a given cell, we first print the background, then the text, then
            # the borders
            # need to subtract 1 because pillow uses inclusive rect coordinates
            right = left + colwidths[r] - 1
            bottom = top + rowheights[b] - 1
            draw.rectangle(
                (left, top, right, bottom),
                cell.background,
            )
            # draw content adding padding
            cell.content.draw(draw, left=left + HORIZONTAL_PADDING, top=top + VERTICAL_PADDING)
            # draw bottom-right border
            draw.line([
                (left, bottom),
                (right, bottom),
                (right, top),
            ], fill=(172, 176, 170))
            if not cell.attached:
                # overdraw previous cell's bottom border
                draw.line([(left, top-1), (right-1, top-1)], fill=ERROR)

            left += colwidths[r]
        top += rowheights[b]

    return im


def render_inconsistent_batch(batch):
    """If a batch has inconsistent targets, just point out the inconsistency by
    listing the PR and targets
    """
    with file_open('web/static/fonts/google/Open_Sans/Open_Sans-Regular.ttf', 'rb') as f:
        font = ImageFont.truetype(f, size=16, layout_engine=0)

    im = Image.new("RGB", (4000, 4000), color=BG['danger'])
    w = h = 0
    def draw(label, draw=ImageDraw.Draw(im)):
        nonlocal w, h

        draw.text((0, h), label, fill=blend(ERROR, 1.0, over=BG['danger']), font=font)

        _, _, ww, hh = font.getbbox(label)
        w = max(w, ww)
        h += hh

    draw(" Inconsistent targets:")
    for p in batch.prs:
        draw(f" • {p.display_name} has target '{p.target.name}'")
    draw(" To resolve, either retarget or close the mis-targeted pull request(s).")

    return im.crop((0, 0, w+10, h+5))



Color = Tuple[int, int, int]
TEXT: Color = (102, 102, 102)
ERROR: Color = (220, 53, 69)
SUCCESS: Color = (40, 167, 69)
BG: Mapping[str | None, Color] = collections.defaultdict(lambda: (255, 255, 255), {
    'info': (217, 237, 247),
    'success': (223, 240, 216),
    'warning': (252, 248, 227),
    'danger': (242, 222, 222),
})


CHECK_MARK = "\uf00c"
CROSS = "\uf00d"
BOX_EMPTY = "\uf096"


def blend_single(c: int, over: int, opacity: float) -> int:
    return round(over * (1 - opacity) + c * opacity)

def blend(color: Color, opacity: float, *, over: Color = (255, 255, 255)) -> Color:
    assert 0.0 <= opacity <= 1.0
    return (
        blend_single(color[0], over[0], opacity),
        blend_single(color[1], over[1], opacity),
        blend_single(color[2], over[2], opacity),
    )

def lighten(color: Color, *, by: float) -> Color:
    # colorsys uses values in the range [0, 1] rather than pillow/CSS-style [0, 225]
    r, g, b = tuple(c / 255 for c in color)
    hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)

    # by% of the way between value and 1.0
    if by >= 0: lightness += (1.0 - lightness) * by
    # -by% of the way between 0 and value
    else:lightness *= (1.0 + by)

    return cast(Color, tuple(
        round(c * 255)
        for c in colorsys.hls_to_rgb(hue, lightness, saturation)
    ))
