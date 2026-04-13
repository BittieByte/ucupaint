import bpy
from bpy.app.handlers import persistent
import os, requests, time, threading, json, socket
from bpy.props import PointerProperty, IntProperty, FloatProperty
import bpy.utils.previews
from .common import get_addon_filepath, is_bl_newer_than, get_addon_title, get_user_preferences
from . import lib

# ---------------------------------------------------------------------------
# Robust cross-platform online check
# ---------------------------------------------------------------------------

# Hosts to probe: (host, port, description)
# We try DNS resolution + a lightweight TCP connect so it works even when
# ICMP (ping) is blocked by a firewall.
_ONLINE_PROBE_HOSTS = [
    ("8.8.8.8",         53,  "Google DNS"),
    ("1.1.1.1",         53,  "Cloudflare DNS"),
    ("208.67.222.222",  53,  "OpenDNS"),
    ("9.9.9.9",         53,  "Quad9 DNS"),
]

_online_cache: bool | None = None          # None = not yet tested
_online_cache_time: float  = 0.0
_ONLINE_CACHE_TTL: float   = 60.0          # re-check at most once per minute
_online_lock = threading.Lock()


def is_online(timeout: float = 2.0) -> bool:
    """
    Return True if the machine appears to have a working internet connection.

    Strategy
    --------
    1. Try a non-blocking TCP connect to several well-known DNS servers on
       port 53.  Port 53 is almost always open outbound and requires no HTTP
       stack, so it is the lightest possible probe.
    2. The result is cached for _ONLINE_CACHE_TTL seconds so repeated calls
       inside one Blender session don't spam the network.
    3. If *every* probe raises OSError (including errno ENETDOWN / ENETUNREACH
       which is what Linux raises when the NIC is disabled) we return False.

    This works on Linux, Windows and macOS without any extra dependencies and
    without relying on ICMP / ping.
    """
    global _online_cache, _online_cache_time

    with _online_lock:
        now = time.monotonic()
        if _online_cache is not None and (now - _online_cache_time) < _ONLINE_CACHE_TTL:
            return _online_cache

        result = False
        for host, port, _ in _ONLINE_PROBE_HOSTS:
            try:
                # AF_INET + SOCK_STREAM = TCP; connect_ex returns 0 on success
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                err = sock.connect_ex((host, port))
                sock.close()
                if err == 0:
                    result = True
                    break
            except OSError:
                # ENETDOWN, ENETUNREACH, ENONET, etc. – interface is down
                continue

        _online_cache = result
        _online_cache_time = now
        return result


def invalidate_online_cache() -> None:
    """Force the next is_online() call to re-probe the network."""
    global _online_cache
    with _online_lock:
        _online_cache = None


# ---------------------------------------------------------------------------
# Thread-safe RNA helpers
# ---------------------------------------------------------------------------

def _main_thread_set_status(goal_ui_ref_getter, status: str) -> None:
    """Schedule a connection_status write on the main thread."""
    def _do():
        try:
            ui = goal_ui_ref_getter()
            if ui is not None:
                ui.connection_status = status
        except Exception:
            pass
        return None  # don't repeat
    bpy.app.timers.register(_do, first_interval=0.0)


def _main_thread_refresh_ui() -> None:
    """Schedule a UI redraw on the main thread."""
    def _do():
        try:
            refresh_ui()
        except Exception:
            pass
        return None
    bpy.app.timers.register(_do, first_interval=0.0)


def _get_goal_ui():
    """Safe accessor for the WindowManager property (main thread only)."""
    try:
        return bpy.context.window_manager.ypui_credits
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class YForceUpdateSponsors(bpy.types.Operator):
    """Force Update Sponsors"""
    bl_idname = "wm.y_force_update_sponsors"
    bl_label = "Force Update Sponsors"

    clear_image_cache: bpy.props.BoolProperty(
        default=False,
        description="Clear image cache",
    )

    use_dummy_users: bpy.props.BoolProperty(
        default=False,
        description="Use dummy users",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'clear_image_cache', text="Clear Image Cache")
        if get_user_preferences().developer_mode:
            layout.prop(self, 'use_dummy_users', text="Use Dummy Users for Testing")

    def execute(self, context):
        path = credits_path
        path_last_check = os.path.join(path, "last_check.txt")

        if os.path.exists(path_last_check):
            os.remove(path_last_check)

        goal_ui = context.window_manager.ypui_credits
        goal_ui.initialized = False
        goal_ui.use_dummy_users = (
            self.use_dummy_users if get_user_preferences().developer_mode else False
        )

        invalidate_online_cache()
        refresh_image_caches(self.clear_image_cache)

        return {'FINISHED'}


class YRefreshSponsors(bpy.types.Operator):
    """Force Refresh Sponsors"""
    bl_idname = "wm.y_force_refresh_sponsors"
    bl_label = "Force Refresh Sponsors"

    def execute(self, context):
        print_info("Force refresh sponsors...")
        path = credits_path
        path_last_check = os.path.join(path, "last_check.txt") # to store last check time

        if os.path.exists(path_last_check):
            os.remove(path_last_check)

        goal_ui = context.window_manager.ypui_credits
        goal_ui.initialized = False
        goal_ui.connection_status = 'INIT'

        invalidate_online_cache()

        return {'FINISHED'}


class YTierPagingButton(bpy.types.Operator):
    """Paging"""
    bl_idname = "wm.y_sponsor_paging"
    bl_label = "Next Page"

    is_next_button: bpy.props.BoolProperty(default=True)
    tier_index:     bpy.props.IntProperty(default=0)
    max_page:       bpy.props.IntProperty(default=0)

    def execute(self, context):
        goal_ui = context.window_manager.ypui_credits
        current_page = goal_ui.page_tiers[self.tier_index]
        if self.is_next_button:
            current_page += 1
            if self.max_page > 0 and current_page >= self.max_page:
                current_page = self.max_page - 1
        else:
            current_page -= 1
            if current_page < 0:
                current_page = 0
        goal_ui.page_tiers[self.tier_index] = current_page
        return {'FINISHED'}


class YCollaboratorPagingButton(bpy.types.Operator):
    """Paging"""
    bl_idname = "wm.y_collaborator_paging"
    bl_label = "Next Page"

    is_next_button: bpy.props.BoolProperty(default=True)
    max_page:       bpy.props.IntProperty(default=0)

    def execute(self, context):
        goal_ui = context.window_manager.ypui_credits
        current_page = goal_ui.page_collaborators
        if self.is_next_button:
            current_page += 1
            if self.max_page > 0 and current_page >= self.max_page:
                current_page = self.max_page - 1
        else:
            current_page -= 1
            if current_page < 0:
                current_page = 0
        goal_ui.page_collaborators = current_page
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class YSponsorPopover(bpy.types.Panel):
    bl_idname = "NODE_PT_ysponsor_popover"
    bl_label = get_addon_title() + " Sponsor Menu"
    bl_description = get_addon_title() + " Sponsor Menu"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_ui_units_x = 15

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        goal = collaborators.sponsorships
        one_time_total = 0
        recurring_total = 0
        print_info("Checking one-time sponsors...", len(collaborators.sponsors))
        for sp in collaborators.sponsors.values():
            if sp["one_time"]:
                one_time_total += sp["amount"]
            else:
                recurring_total += sp["amount"]

        if one_time_total > 0:
            print_info("One-time sponsors total this month: $" + str(one_time_total))

        if recurring_total > 0:
            print_info("Recurring sponsors total: $" + str(recurring_total))

        desc = goal.get('description', '')

        daily_row = layout.row()
        daily_row.label(text="Only counting recurring sponsors (updated daily).")
        daily_row.operator('wm.y_force_update_sponsors', text="", icon='FILE_REFRESH')

        layout.separator()

        row_quote = layout.row()
        char_per_line = 40
        split_desc = desc.split(' ')
        current_text = '"'

        maintaner = goal.get('maintainer')
        user_maintaner = collaborators.contributors.get(maintaner, None)
        maintainer_icon = user_maintaner['thumb'] if user_maintaner else 0
        if maintainer_icon:
            row_quote.template_icon(icon_value=maintainer_icon, scale=3.0)
        col = row_quote.column(align=True)
        for d in split_desc:
            if len(current_text + d) > char_per_line:
                col.label(text=current_text)
                current_text = ''
            current_text += d + ' '
        col.label(text=current_text + "\"")
        col.label(text="~ " + maintaner)


# ---------------------------------------------------------------------------
# Property group
# ---------------------------------------------------------------------------

class YSponsorProp(bpy.types.PropertyGroup):
    progress: FloatProperty(
        default=0.0,
        min=0.0,
        max=100.0,
        description='Only counting recurring sponsors',
        subtype='PERCENTAGE'
    )

    expand_tiers: bpy.props.BoolVectorProperty(
        name="Expand Tiers",
        description="Expand Tiers List",
        size=8,
    )

    page_tiers: bpy.props.IntVectorProperty(
        name="Page Tiers",
        description="Page Tiers",
        size=8,
    )

    page_collaborators: IntProperty(default=0)

    expand_description: bpy.props.BoolProperty(
        default=False,
        description=get_addon_title() + "'s sponsor is updated daily",
    )

    initialized: bpy.props.BoolProperty(default=False)

    expanded: bpy.props.BoolProperty(default=False)

    connection_status: bpy.props.EnumProperty(
        name='connection status',
        items=(
            ('INIT',       "INIT",       'Initial'),
            ('REQUESTING', "REQUESTING", 'Requesting'),
            ('SUCCESS',    "SUCCESS",    'Success'),
            ('FAILED',     'FAILED',     "Failed"),
        ),
        default='INIT'
    )

    use_dummy_users: bpy.props.BoolProperty(
        default=False,
        description="Use dummy users",
    )


# ---------------------------------------------------------------------------
# Support panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_YPaint_support_ui(bpy.types.Panel):
    bl_idname = "VIEW3D_PT_ypaint_support_ui"
    bl_label = "Support " + get_addon_title()
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_ui_units_x = 13

    def draw_multiline(self, layout, text: str, panel_width: int):
        all_desc = text.split(' ')
        column = layout.column(align=True)
        current_text = ''
        for d in all_desc:
            if len(current_text + d) > panel_width // 15:
                column.label(text=current_text)
                current_text = ''
            current_text += d + ' '
        column.label(text=current_text)

    def draw_expanding_title(self, layout, expand, object, prop_name, title):
        icon = 'DOWNARROW_HLT' if expand else 'RIGHTARROW'
        row = layout.row(align=True)
        rrow = row.row(align=True)
        if is_bl_newer_than(2, 80):
            rrow.alignment = 'LEFT'
            rrow.scale_x = 0.95
            rrow.prop(object, prop_name, emboss=False, text=title, icon=icon)
        else:
            rrow.prop(object, prop_name, emboss=False, text='', icon=icon)
            rrow.label(text=title)
        return row

    def draw_tier_title(self, layout, expand, object, prop_name, title, index_prop, icon_val):
        icon = 'DOWNARROW_HLT' if expand else 'RIGHTARROW'
        row = layout.row(align=True)
        rrow = row.row(align=True)
        if is_bl_newer_than(2, 80):
            rrow.alignment = 'LEFT'
            rrow.scale_x = 0.95
            rrow.prop(object, prop_name, index=index_prop, emboss=False, text='', icon=icon)
            rrow.prop(object, prop_name, index=index_prop, text=title,
                      icon_value=lib.get_icon(icon_val, 'REC'), emboss=False)
        else:
            rrow.prop(object, prop_name, index=index_prop, emboss=False, text='', icon=icon)
        return row

    def draw_header_preset(self, context):
        goal_ui = context.window_manager.ypui_credits
        if not goal_ui.expanded:
            layout = self.layout
            row = layout.row(align=True)
            url = collaborators.sponsorships.get('url', collaborators.default_url)
            row.operator('wm.url_open', text="Donate Us", icon='FUND').url = url
            if get_user_preferences().developer_mode:
                row.operator('wm.y_force_update_sponsors', text="", icon='FILE_REFRESH')
        goal_ui.expanded = False

    def draw_item(self, layout, icon, label, url='', scale_icon: float = 3.0,
                  horizontal_mode: bool = True):
        if horizontal_mode:
            row = layout.row(align=True)
            row.alignment = 'LEFT'
            if scale_icon != 0.0:
                row.template_icon(icon_value=icon, scale=scale_icon)
                btn_row = row.row(align=True)
                btn_row.scale_y = scale_icon
                btn_url = btn_row.operator('wm.url_open', text=label, emboss=False)
                btn_url.url = url
            else:
                row.label(text=label)
        else:
            col = layout.column(align=True)
            col.template_icon(icon_value=icon, scale=scale_icon)
            col.operator('wm.url_open', text=label, emboss=False).url = url

    def draw_empty_member(self, layout, url, scale_icon: float = 3.0,
                          horizontal_mode: bool = True):
        content = 'No sponsors yet, be the first one!'
        if horizontal_mode:
            row = layout.row(align=True)
            if scale_icon != 0.0:
                row.alignment = 'LEFT'
                row.template_icon(icon_value=collaborators.empty_pic, scale=scale_icon)
                btn_row = row.row(align=True)
                btn_row.scale_y = scale_icon
                btn_url = btn_row.operator('wm.url_open', text=content, emboss=False)
                btn_url.url = url
            else:
                row.label(text=content)
        else:
            col = layout.column(align=True)
            row = col.row(align=True)
            row.alignment = 'LEFT'
            row.template_icon(icon_value=collaborators.empty_pic, scale=scale_icon)
            row = col.row(align=True)
            row.alignment = 'LEFT'
            row.scale_x = 0.95
            row.operator('wm.url_open', text=content, emboss=False).url = url

    def draw_tier_members(self, panel_width, goal_ui, layout, title: str, icon_val,
                          tier_index: int, per_column: int = 3, current_page: int = 0,
                          per_page_item: int = 4, scale_icon: float = 3.0,
                          horizontal_mode: bool = True):

        filtered_items = [
            item for item in collaborators.sponsors.values()
            if item['tier'] == tier_index and item['public']
        ]

        member_count = len(filtered_items)

        stripped_title = ''.join(c for c in title if ord(c) < 128).strip()
        text_object = stripped_title
        if member_count > 0:
            text_object += ' (' + str(member_count) + ')'

        expand = goal_ui.expand_tiers[tier_index]
        title_row = self.draw_tier_title(layout, expand, goal_ui, 'expand_tiers',
                                         text_object, tier_index, icon_val)
        paging_layout = title_row.row(align=True)
        paging_layout.alignment = 'RIGHT'

        if per_page_item < per_column:
            per_page_item = per_column

        if expand:
            row = layout.row(align=True)
            row.label(text='', icon='BLANK1')
            box = row.box()
            if member_count == 0:
                url = collaborators.sponsorships.get('url', "")
                col_box = box.column(align=True)
                self.draw_empty_member(col_box, url, scale_icon, horizontal_mode)
            else:
                grid = box.grid_flow(row_major=True, columns=per_column,
                                     even_columns=True, even_rows=True, align=True)

                counter_member = 0
                paged_items = filtered_items[
                    current_page * per_page_item:(current_page + 1) * per_page_item
                ]

                for item in paged_items:
                    counter_member += 1
                    thumb = item['thumb'] or collaborators.loading_pic
                    id = item["name"] or item['id'].strip()
                    if item['one_time']:
                        id += "*" if horizontal_mode else "*" + id
                    self.draw_item(grid, thumb, id, item["url"], scale_icon, horizontal_mode)

                missing_column = per_column - (counter_member % per_column)
                if missing_column != per_column:
                    for _ in range(missing_column):
                        self.draw_item(grid, collaborators.default_pic, '', '',
                                       scale_icon, horizontal_mode)

            if member_count > per_page_item:
                prev = paging_layout.operator('wm.y_sponsor_paging', text='', icon='TRIA_LEFT')
                prev.is_next_button = False
                prev.tier_index = tier_index
                prev.max_page = (member_count + per_page_item - 1) // per_page_item

                paging_layout.label(text=str(current_page + 1) + '/' + str(prev.max_page))

                next_ = paging_layout.operator('wm.y_sponsor_paging', text='', icon='TRIA_RIGHT')
                next_.is_next_button = True
                next_.tier_index = tier_index
                next_.max_page = prev.max_page

    def draw(self, context):
        region = context.region
        panel_width = region.width
        layout = self.layout

        row = layout.row()
        row.alignment = 'CENTER'
        row.label(text='Support ' + get_addon_title() + '!', icon='ARMATURE_DATA')

        goal = collaborators.sponsorships
        goal_ui = context.window_manager.ypui_credits
        url_donation = collaborators.default_url

        if goal and 'targetValue' in goal:
            url_donation = goal.get('url', url_donation)

            row_title = layout.row(align=True)
            row_title.alignment = 'CENTER'
            row_title.label(text=get_addon_title() + "'s goal : $" +
                            str(goal['targetValue']) + "/month")

            target = goal['targetValue']
            donation = sum(i['amount'] for i in collaborators.sponsors.values()
                           if not i['one_time'])
            percentage = 100 * donation / target

            goal_ui.progress = percentage

            progress_row = layout.row(align=True)
            progress_row.prop(goal_ui, 'progress', text='$' + str(donation), slider=True)
            progress_row.popover("NODE_PT_ysponsor_popover", text='', icon='QUESTION')

        don_col = layout.column(align=True)
        don_col.scale_y = 1.5
        don_col.operator('wm.url_open', text="Become a Sponsor", icon='FUND').url = url_donation

        check_contributors(goal_ui)

        if is_online() and 'tiers' in goal and goal_ui.connection_status != "REQUESTING":
            layout.separator()
            layout.label(text="Our Sponsors :", icon='HEART')

            tiers = goal.get('tiers', [])
            if tiers:
                for tier in reversed(tiers):
                    idx = tiers.index(tier)
                    scale_icon     = tier.get('scale', 3)
                    horizontal_mode = tier.get('horizontal', True)
                    column_count   = max(tier.get('column_num', 1), 1)
                    self.draw_tier_members(
                        panel_width, goal_ui, layout,
                        tier['name'], tier['icon_value'], idx,
                        column_count, goal_ui.page_tiers[idx],
                        tier['per_page_item'], scale_icon, horizontal_mode,
                    )

            for item in collaborators.sponsors.values():
                if item['one_time'] and item['public']:
                    tier = item['tier']
                    if goal_ui.expand_tiers[tier]:
                        layout.separator()
                        layout.label(text="* One-time sponsors")
                        break

        elif is_online():
            if goal_ui.connection_status == "REQUESTING":
                layout.label(text="Loading data...", icon='TIME')
            elif goal_ui.connection_status == "FAILED":
                layout.label(text="Failed to load data.", icon='ERROR')
                layout.operator('wm.y_force_refresh_sponsors', text='Reload sponsors',
                                icon='FILE_REFRESH')

        else:
            layout.label(text="No internet access, can't load sponsors.", icon='ERROR')

        goal_ui.expanded = True

        if get_user_preferences().developer_mode:
            layout.operator('wm.y_force_update_sponsors', text="Force Update Sponsors",
                            icon='FILE_REFRESH')


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def print_info(*args):
    if get_user_preferences().developer_mode:
        print(*args)


def print_error(*args):
    if get_user_preferences().developer_mode:
        print("ERROR:", *args)


# ---------------------------------------------------------------------------
# Image / preview helpers
# ---------------------------------------------------------------------------

def is_valid_file(path: str) -> bool:
    """Return True only if path exists, is a regular file, and is non-empty."""
    try:
        return os.path.exists(path) and os.path.isfile(path) and os.stat(path).st_size > 0
    except Exception:
        return False


def load_preview(key: str, file_name: str):
    """
    Load a preview image safely.  If the file is missing or zero-byte we skip
    it rather than letting Blender's C layer segfault on a corrupt buffer.
    """
    if not is_valid_file(file_name):
        print_info("load_preview: skipping missing/empty file", file_name)
        return None
    try:
        if key in previews_users:
            return previews_users[key]
        return previews_users.load(key, file_name, 'IMAGE', True)
    except Exception as e:
        print_error("load_preview failed for", file_name, ":", e)
        return None


def refresh_image_caches(force_reload: bool = False) -> None:
    path = credits_path
    folders = os.path.join(path, "icons", "contributors")
    if not os.path.exists(folders):
        os.makedirs(folders)

    is_expired = False
    path_last_check = os.path.join(path, "last_check_images.txt")
    current_time = time.time()

    if not force_reload:
        if is_valid_file(path_last_check):
            with open(path_last_check, "r", encoding="utf-8") as f:
                try:
                    last_check = float(f.read().strip())
                except ValueError:
                    last_check = 0.0
                if current_time - last_check > 24 * 60 * 60 * 30:
                    is_expired = True
        else:
            is_expired = True
    else:
        is_expired = True

    if is_expired:
        with open(path_last_check, "w", encoding="utf-8") as f:
            f.write(str(current_time))

        for fname in os.listdir(folders):
            file_path = os.path.join(folders, fname)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
            except Exception as e:
                print_info("Error removing file " + file_path + ": " + str(e))


# ---------------------------------------------------------------------------
# Contributor / sponsor loading
# ---------------------------------------------------------------------------

def check_contributors(goal_ui: YSponsorProp) -> None:
    if is_online():
        if not goal_ui.initialized:
            goal_ui.initialized = True
            print_info("first time init, loading contributors...")
            load_thread = threading.Thread(target=load_contributors, args=(goal_ui,),
                                           daemon=True)
            load_thread.start()
        else:
            load_expanded_images(goal_ui)
    elif goal_ui.initialized:
        goal_ui.initialized = False


def load_local_contributors() -> None:
    path = credits_path
    path_contributors = os.path.join(path, "contributors.csv")
    folders = os.path.join(path, "icons", "contributors")

    content = ""
    if is_valid_file(path_contributors):
        with open(path_contributors, "r", encoding="utf-8") as f:
            content = f.read()

    collaborators.contributors.clear()
    if not content:
        return

    skip_header = True
    for line in content.strip().splitlines():
        if skip_header:
            skip_header = False
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 4:
            contributor = {
                'id':        parts[0],
                'name':      parts[1],
                'url':       parts[2],
                'image_url': parts[3],
                'thumb':     None,
            }
            file_name = os.path.join(folders, contributor['id'] + '.png')
            img = load_preview(contributor['id'], file_name)
            if img is not None:
                contributor['thumb'] = img.icon_id
            collaborators.contributors[contributor['id']] = contributor


def load_contributors(goal_ui: YSponsorProp) -> None:
    """
    Background-thread entry point.
    All bpy RNA writes are funnelled through _main_thread_set_status() so that
    we never touch RNA from outside the main thread.
    """
    path = credits_path
    if not os.path.exists(path):
        os.makedirs(path)

    path_last_check      = os.path.join(path, "last_check.txt") # to store last check time
    path_contributors    = os.path.join(path, "contributors.csv")
    path_sponsors        = os.path.join(path, "sponsors.csv")
    path_sponsorship_goal = os.path.join(path, "credits.json")

    current_time = time.time()

    # ---- decide whether we need to re-download --------------------------------
    reload_contributors = True
    if is_valid_file(path_last_check):
        try:
            with open(path_last_check, "r", encoding="utf-8") as f:
                last_check = float(f.read().strip())
            span_time = current_time - last_check
            if span_time <= 24 * 60 * 60:
                span_hours = span_time / 3600
                fmt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_check))
                print_info(f'Last check {span_hours:.2f}h ago at {fmt}. Not reloading.')
                reload_contributors = False
        except ValueError:
            pass

    if not is_valid_file(path_contributors):
        reload_contributors = True
    if not is_valid_file(path_sponsors):
        reload_contributors = True
    if not is_valid_file(path_sponsorship_goal):
        reload_contributors = True

    # ---- load cached files (may be overwritten below) -------------------------
    content               = ""
    content_sponsors      = ""
    content_sponsorship_goal_str = ""

    if is_valid_file(path_contributors):
        with open(path_contributors, "r", encoding="utf-8") as f:
            content = f.read()

    if is_valid_file(path_sponsors):
        with open(path_sponsors, "r", encoding="utf-8") as f:
            content_sponsors = f.read()

    if is_valid_file(path_sponsorship_goal):
        with open(path_sponsorship_goal, "r", encoding="utf-8") as f:
            content_sponsorship_goal_str = f.read()
        try:
            settings = json.loads(content_sponsorship_goal_str)
            collaborators.sponsorships        = settings["sponsorships"]
            collaborators.contributor_settings = settings.get("contributors", {})
        except Exception as e:
            print_error("Failed to parse credits.json:", e)

    # ---- network fetch if needed ---------------------------------------------
    if reload_contributors and is_online():
        timeout_seconds = 10
        _main_thread_set_status(_get_goal_ui, "REQUESTING")
        data_url = "https://raw.githubusercontent.com/ucupumar/ucupaint-wiki/master/data/"

        try:
            print_info("Reloading contributors...")
            response = requests.get(data_url + "contributors.csv", timeout=timeout_seconds)
            if response.status_code == 200:
                content = response.text
                print_info("Response:", content)
                with open(path_contributors, "w", encoding="utf-8") as f:
                    f.write(content)

            print_info("Reloading sponsors...")
            response = requests.get(data_url + "sponsors.csv", timeout=timeout_seconds)
            if response.status_code == 200:
                content_sponsors = response.text
                print_info("Response:", content_sponsors)
                with open(path_sponsors, "w", encoding="utf-8") as f:
                    f.write(content_sponsors)

            print_info("Reloading sponsorship goal...")
            response = requests.get(data_url + "credits.json", timeout=timeout_seconds)
            if response.status_code == 200:
                content_sponsorship_goal_str = response.text
                print_info("Response credits:", content_sponsorship_goal_str)
                with open(path_sponsorship_goal, "w", encoding="utf-8") as f:
                    f.write(content_sponsorship_goal_str)
                try:
                    settings = json.loads(content_sponsorship_goal_str)
                    collaborators.sponsorships        = settings["sponsorships"]
                    collaborators.contributor_settings = settings.get("contributors", {})
                except Exception as e:
                    print_error("Failed to parse downloaded credits.json:", e)

            with open(path_last_check, "w", encoding="utf-8") as f:
                f.write(str(time.time()))

            _main_thread_set_status(_get_goal_ui, "SUCCESS")

        except requests.exceptions.ReadTimeout:
            print_info("timeout request")
            _main_thread_set_status(_get_goal_ui, "FAILED")
        except requests.exceptions.ConnectionError:
            print_info("connection error")
            _main_thread_set_status(_get_goal_ui, "FAILED")
        except Exception as e:
            print_error("Unexpected error during network fetch:", e)
            _main_thread_set_status(_get_goal_ui, "FAILED")

    elif reload_contributors:
        # Needed a reload but we have no internet and no valid cache
        _main_thread_set_status(_get_goal_ui, "FAILED")

    # ---- parse contributors --------------------------------------------------
    collaborators.contributors.clear()
    skip_header = True
    for line in content.strip().splitlines():
        if skip_header:
            skip_header = False
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 4:
            collaborators.contributors[parts[0]] = {
                'id':        parts[0],
                'name':      parts[1],
                'url':       parts[2],
                'image_url': parts[3],
                'thumb':     None,
            }

    # ---- parse sponsors ------------------------------------------------------
    collaborators.sponsors.clear()
    skip_header = True
    for line in content_sponsors.strip().splitlines():
        if skip_header:
            skip_header = False
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 9:
            try:
                sponsor = {
                    'id':        parts[0],
                    'name':      parts[1],
                    'url':       parts[2],
                    'image_url': parts[3],
                    'amount':    float(parts[5]) if parts[5] else 0.0,
                    'one_time':  parts[6] == 'True',
                    'tier':      int(parts[7]),
                    'public':    parts[8] == 'True',
                    'thumb':     None,
                }
                collaborators.sponsors[sponsor['id']] = sponsor
                print_info("Loaded sponsor", sponsor['id'], "=", str(sponsor))
            except (ValueError, IndexError) as e:
                print_error("Failed to parse sponsor line:", line, ":", e)

    # ---- tier expand setup ---------------------------------------------------
    expanding_top_tier = collaborators.sponsorships.get('expanding_tier_member', 3)
    tiers = collaborators.sponsorships.get('tiers', [])
    total_tiers = len(tiers)

    # Tier expand must be set from main thread
    expand_states = [False] * 8
    for idx in range(total_tiers):
        i = total_tiers - 1 - idx
        if expanding_top_tier > 0:
            member_count = sum(
                1 for item in collaborators.sponsors.values()
                if item['tier'] == i and item['public']
            )
            if member_count > 0:
                expand_states[i] = True
                expanding_top_tier -= 1

    def _apply_expand():
        try:
            ui = _get_goal_ui()
            if ui:
                for i, val in enumerate(expand_states):
                    ui.expand_tiers[i] = val
        except Exception:
            pass
        return None

    bpy.app.timers.register(_apply_expand, first_interval=0.0)

    refresh_image_caches()
    _main_thread_refresh_ui()

    # ---- developer dummy data ------------------------------------------------
    if get_user_preferences().developer_mode and goal_ui.use_dummy_users:
        tier_count = len(tiers)
        dummy_multiplier = 3
        for m in range(dummy_multiplier):
            for i, contributor in enumerate(list(collaborators.contributors.values())):
                random_num = hash(contributor['id']) % 1000
                new_c = contributor.copy()
                new_c['tier']     = i % tier_count
                new_c['one_time'] = (random_num % 2) == 0
                new_c['public']   = True
                new_c['amount']   = ((random_num % 20) + 1) * (new_c['tier'] + 1) * 5
                new_c['id']       = contributor['id'] + str(m)
                new_c['name']     = new_c['id']
                collaborators.sponsors[new_c['id']] = new_c
                print_info("Added dummy sponsor", new_c['id'], "=", str(new_c))

    print_info("loaded contributors and sponsors.")
    load_expanded_images(goal_ui)


def load_expanded_images(goal_ui: YSponsorProp) -> None:
    if collaborators.load_thread and collaborators.load_thread.is_alive():
        return

    cont_setting = collaborators.contributor_settings
    current_page = goal_ui.page_collaborators
    per_page     = cont_setting.get('per_page_item', 12)
    icon_size    = cont_setting.get('icon_size', 0)

    paged_contributors = list(collaborators.contributors.values())[
        current_page * per_page:(current_page + 1) * per_page
    ]

    to_load = []  # list of (url, file_path, id)

    path    = credits_path
    folders = os.path.join(path, "icons", "contributors")
    if not os.path.exists(folders):
        os.makedirs(folders)

    for c in paged_contributors:
        if c['thumb'] is None:
            file_name = os.path.join(folders, c['id'] + '.png')
            link = c['image_url'] + "&s=" + str(icon_size)
            to_load.append((link, file_name, c['id']))

    tiers = collaborators.sponsorships.get('tiers', [])
    for i, tier in enumerate(tiers):
        if tier.get('icon_size', 0) <= 0 or not goal_ui.expand_tiers[i]:
            continue

        cur_page  = goal_ui.page_tiers[i]
        per_page_i = tier.get('per_page_item', 4)
        paged_sp  = [s for s in collaborators.sponsors.values()
                     if s['tier'] == i and s['public']]
        paged_sp  = paged_sp[cur_page * per_page_i:(cur_page + 1) * per_page_i]

        sp_icon_size = tier.get('icon_size', 0)
        for sp in paged_sp:
            if sp['thumb'] is None:
                file_name = os.path.join(folders, sp['id'] + '.png')
                link = sp['image_url'] + "&s=" + str(sp_icon_size)
                to_load.append((link, file_name, sp['id']))

    print_info("to load images:", len(to_load))
    if to_load:
        links      = [t[0] for t in to_load]
        file_names = [t[1] for t in to_load]
        ids        = [t[2] for t in to_load]
        collaborators.load_thread = threading.Thread(
            target=download_stream,
            args=(links, file_names, ids, 20),
            daemon=True,
        )
        collaborators.load_thread.start()


def download_stream(links, file_names, ids, timeout: int = 10) -> None:
    """
    Download avatar images in a background thread.
    Icon IDs are assigned via a timer to keep RNA writes on the main thread.
    """
    print_info("Downloading", len(links), "images...")

    for idx, file_name in enumerate(file_names):
        k = ids[idx]

        if not os.path.exists(file_name):
            if is_online():
                link = links[idx]
                try:
                    response = requests.get(link, stream=True, timeout=timeout)
                    total_length = response.headers.get('content-length')
                    if not total_length:
                        print_info('Error #1 while downloading', link, ': Empty Response.')
                        continue
                    total_length = int(total_length)
                    with open(file_name, "wb") as f:
                        for data in response.iter_content(chunk_size=4096):
                            f.write(data)
                except Exception as e:
                    print_info('Error #2 while downloading', link, ':', str(e))
                    # Remove partial file so we don't try to load a corrupt one
                    try:
                        if os.path.exists(file_name):
                            os.remove(file_name)
                    except Exception:
                        pass
                    continue
            else:
                continue

        # Validate before loading
        if not is_valid_file(file_name):
            print_info("Skipping invalid/empty downloaded file:", file_name)
            continue

        img = load_preview(k, file_name)
        if img is None:
            continue

        icon_id = img.icon_id

        # Assign thumb on the main thread
        def _assign(key=k, iid=icon_id):
            try:
                if key in collaborators.contributors:
                    collaborators.contributors[key]['thumb'] = iid
                if key in collaborators.sponsors:
                    collaborators.sponsors[key]['thumb'] = iid
                refresh_ui()
            except Exception:
                pass
            return None

        bpy.app.timers.register(_assign, first_interval=0.0)

    collaborators.load_thread = None


def refresh_ui() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for reg in area.regions:
                    if reg.type == "UI" and reg.width > 1:
                        reg.tag_redraw()
                    if reg.type == "WINDOW":
                        reg.tag_redraw()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    VIEW3D_PT_YPaint_support_ui,
    YSponsorProp,
    YTierPagingButton,
    YSponsorPopover,
    YForceUpdateSponsors,
    YRefreshSponsors,
    YCollaboratorPagingButton,
]


class Collaborators:
    default_pic         = None
    empty_pic           = None
    loading_pic         = None
    contributors        = {}
    sponsors            = {}
    sponsorships        = {}
    contributor_settings = {}
    load_thread         = None
    default_url         = ""
    default_maintainer  = ""
    default_contributors_url = ""


def get_collaborators():
    return collaborators


@persistent
def check_contributors_on_load(scn):
    goal_ui = bpy.context.window_manager.ypui_credits
    invalidate_online_cache()
    check_contributors(goal_ui)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    global previews_users, collaborators, credits_path

    credits_path = os.path.join(get_addon_filepath(), "credits")
    icon_path    = os.path.join(get_addon_filepath(), "icons")

    previews_users = bpy.utils.previews.new()
    collaborators  = Collaborators()

    def _safe_load_icon(key, rel_path):
        full = os.path.join(icon_path, rel_path)
        img  = load_preview(key, full)
        return img.icon_id if img is not None else 0

    collaborators.default_pic  = _safe_load_icon('blank',   'blank.png')
    collaborators.empty_pic    = _safe_load_icon('empty',   'empty.png')
    collaborators.loading_pic  = _safe_load_icon('loading', 'loading.png')

    collaborators.contributors        = {}
    collaborators.sponsors            = {}
    collaborators.sponsorships        = {}
    collaborators.load_thread         = None
    collaborators.default_url         = "https://github.com/sponsors/ucupumar"
    collaborators.default_maintainer  = "ucupumar"
    collaborators.default_contributors_url = (
        'https://github.com/ucupumar/ucupaint/graphs/contributors'
    )

    load_local_contributors()

    bpy.types.WindowManager.ypui_credits = PointerProperty(type=YSponsorProp)

    ui_sp = bpy.context.window_manager.ypui_credits
    ui_sp.initialized = False

    if is_bl_newer_than(2, 80):
        check_contributors(ui_sp)
        bpy.app.handlers.load_post.append(check_contributors_on_load)


def unregister():
    for cls in classes:
        bpy.utils.unregister_class(cls)

    del bpy.types.WindowManager.ypui_credits

    global previews_users
    bpy.utils.previews.remove(previews_users)
    previews_users = None

    if is_bl_newer_than(2, 80):
        bpy.app.handlers.load_post.remove(check_contributors_on_load)