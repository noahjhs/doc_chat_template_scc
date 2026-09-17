import pandas as pd
import streamlit as st

from utils.auth import (
    add_policy_layer_to_host,
    create_policy_layer,
    create_policy_layer_rule,
    current_token,
    delete_policy_layer,
    delete_policy_layer_rule,
    list_hosts,
    list_policy_layers,
    remove_policy_layer_from_host,
    rename_policy_layer,
    reorder_policy_layer_rules,
    require_agent_session,
    require_app_subdomain,
    update_policy_layer_rule,
)
from utils.policy import BLACKLIST_MATCHES_NOTHING, describe_rule
from utils.sidebar import _fetch_local_json, handle_sign_out_if_requested, render_sidebar
from utils.topbar import render_topbar

st.set_page_config(page_title="Casper - Policy Layers", page_icon="👻", initial_sidebar_state="expanded")
require_app_subdomain()

# Handles (and st.stop()s on) an in-flight sign-out -- see
# utils/sidebar.py's own docstring for why this has to run before
# require_agent_session() below, not after.
handle_sign_out_if_requested()

username = require_agent_session()
render_topbar()
# Captured (not discarded, unlike most other callers of render_sidebar) so
# _refresh() below can push a live re-fetch to every connected daemon --
# see its own docstring for why that's necessary.
local_agent_configs, _ = render_sidebar(username)

st.title("Policy Layers")
st.caption(
    "A Policy Layer is a named, ordered list of rules for how the assistant may invoke a CLI "
    "command on a host. Rules are checked in order, first match wins; if nothing matches, "
    'the call is denied. Each rule constrains the binary and its arguments ("positional '
    'constraints"), any options, and a tier -- "Allow" runs automatically, "Ask" needs your '
    'approval in chat first, "Deny" always rejects it. See the "Resources" plan for the full '
    "design -- addressable directories (in the sidebar's Workspace section) are the other "
    "kind of Resource."
)

AUTH_DOMAIN = st.secrets["AUTH_SERVICE_DOMAIN"]
TOKEN = current_token()

# Deliberately the same "_hosts" session_state key pages/chat.py and
# pages/environments.py read -- so a change made here is immediately what
# they see too, next time it loads, with no separate cache to go stale.
if "_hosts" not in st.session_state:
    st.session_state["_hosts"] = (list_hosts(AUTH_DOMAIN, TOKEN) or {}).get("hosts", [])
if "_policy_layers" not in st.session_state:
    st.session_state["_policy_layers"] = (list_policy_layers(AUTH_DOMAIN, TOKEN) or {}).get("policy_layers", [])


def _refresh():
    # Called only right after a change actually succeeded (every call site
    # is inside the non-error branch of a mutation) -- so it doubles as the
    # signal for the "Update saved" message at the bottom of the page.
    # Also pushes a live refresh to every currently connected daemon (fire-
    # and-forget, same posture as the sidebar's own add_directory call) --
    # without this, a daemon that was already running before a policy layer
    # was created/edited/attached keeps enforcing its own stale cached copy
    # until its next pairing/resume, which made testing a fresh change look
    # like a broken feature rather than an unrelated staleness gap
    # (confirmed directly, for the old command-template version of this
    # same mechanism: a brand-new template came back "unknown" from the
    # daemon until this was added). Broadcasting to every connected host
    # rather than just the one(s) actually affected by this particular
    # mutation is deliberately simple -- an update/delete can affect every
    # host a layer is attached to, and re-fetching is cheap and idempotent,
    # so there's no real cost to over-broadcasting.
    for config in local_agent_configs.values():
        _fetch_local_json(config, "refresh_policy_layers")
    st.session_state.pop("_hosts", None)
    st.session_state.pop("_policy_layers", None)
    st.session_state["_just_saved"] = True


hosts = st.session_state["_hosts"]
policy_layers = st.session_state["_policy_layers"]

TIER_OPTIONS = ["deny", "ask", "allow"]
TIER_LABELS = {"deny": "Deny", "ask": "Ask", "allow": "Allow"}


def _cell_str(value):
    """A row dict here came from a data_editor's DataFrame via
    .to_dict("records") -- a blank text cell round-trips as float NaN
    there, not None or "" (confirmed directly: a column seeded with a mix
    of real strings and None ends up dtype "str", but NaN is still what a
    None cell exports as), so `row.get(...) or ""` alone isn't enough --
    `nan or ""` is still `nan` (NaN is truthy), and calling .strip() on
    that raises. pd.isna() is the one check that's correct for all of
    None/NaN/NaT without also misfiring on a real, non-empty string."""
    return "" if pd.isna(value) else str(value)


def _positional_rows(constraints):
    # Shown as-is, sentinel included -- no hiding it as a blank cell. The
    # table's own TextColumn `default` (see _render_rule_editor) already
    # pre-populates a brand-new row with this same literal text, so what's
    # in the box always matches what's stored; no separate "friendly
    # display" translation layer to keep in sync with that.
    return [{"whitelist": c.get("whitelist") or "", "blacklist": c.get("blacklist") or BLACKLIST_MATCHES_NOTHING} for c in constraints]


def _build_positional_constraints(rows):
    """Mirrors PolicyLayerRuleCreateRequest's own validators client-side, so
    a mistake shows up immediately here rather than only after a round
    trip to auth_service -- the server re-validates regardless, so this is
    purely for faster feedback, never the source of truth. Every row is
    just {whitelist, blacklist}, the one shape a positional constraint
    takes -- a row with both left blank means "value not required" (a
    missing value is matched as "", which an empty whitelist always
    matches and the default blacklist never does), no "*" sentinel
    needed. Sends exactly what's in the box (after stripping), never
    None and never silently substituting a default at save time -- the
    table's own TextColumn `default` is what makes a blank blacklist
    cell actually contain BLACKLIST_MATCHES_NOTHING already, so there's
    nothing left to paper over here; a row where the author deliberately
    cleared it to truly empty is sent as "" as asked, blacklist-matches-
    everything consequence and all."""
    result = [
        {"whitelist": _cell_str(row.get("whitelist")).strip(), "blacklist": _cell_str(row.get("blacklist")).strip()}
        for row in rows
    ]
    errors = []
    # Position 0 (the binary) is optional exactly like every other
    # position -- an empty list here means fully unconstrained, same as
    # any position beyond the list's length already is.
    if result and result[0].get("whitelist") == "{roots}":
        errors.append('Position 0 (the binary) can never use "{roots}" -- it is never a path.')
    return result, errors


def _option_rows(constraints):
    rows = []
    for c in constraints:
        pattern = c.get("pattern") or {}
        rows.append(
            {
                "short": c.get("short") or "",
                "long": c.get("long") or "",
                "whitelist": pattern.get("whitelist") or "",
                "blacklist": pattern.get("blacklist") or BLACKLIST_MATCHES_NOTHING,
            }
        )
    return rows


def _build_option_constraints(rows):
    """A row with neither short nor long filled in is treated as an unused
    placeholder and silently skipped -- option_constraints=[] (no options
    at all) is perfectly valid. Unlike positional_constraints, an included
    option entry always requires that option to be PRESENT -- only the
    value it's allowed to carry is what's optional to constrain here.
    Both whitelist/blacklist left blank means any/no value is accepted; a
    missing value is matched as the empty string (see
    utils/policy_layers.py or the daemon's ruleMatches), so a whitelist of
    "^$" requires the option be present with NO value. Sends exactly
    what's in the box (after stripping) for both fields, same WYSIWYG
    posture as _build_positional_constraints above."""
    result = []
    for row in rows:
        short = _cell_str(row.get("short")).strip() or None
        long = _cell_str(row.get("long")).strip() or None
        if not short and not long:
            continue
        whitelist = _cell_str(row.get("whitelist")).strip()
        blacklist = _cell_str(row.get("blacklist")).strip()
        result.append({"short": short, "long": long, "pattern": {"whitelist": whitelist, "blacklist": blacklist}})
    return result, []


def _uses_roots(positional_constraints, option_constraints):
    if any(c.get("whitelist") == "{roots}" for c in positional_constraints):
        return True
    return any((c.get("pattern") or {}).get("whitelist") == "{roots}" for c in option_constraints)


def _render_rule_editor(layer, rule):
    """The create-or-edit form for one rule -- rule is None when creating
    a brand-new one (appended to the end of the layer), otherwise the
    existing rule being edited. Both positional and option constraints use
    st.data_editor(num_rows="dynamic") so adding/removing a row directly
    changes the constraint list's length -- for positional constraints,
    row order IS argv position (row 0 = the binary); option row order
    doesn't matter."""
    is_new = rule is None
    key_suffix = "new" if is_new else rule["id"]
    blank_positional_row = {"whitelist": "", "blacklist": BLACKLIST_MATCHES_NOTHING}
    # Position 0 (the binary) is optional, same as every other position --
    # an empty list here is a fully legitimate, intentional "unconstrained"
    # state, not a placeholder needing a phantom blank row.
    saved_positional_rows = [] if is_new else _positional_rows(rule["positional_constraints"])
    saved_option_rows = [] if is_new else _option_rows(rule["option_constraints"])
    tier_default = "ask" if is_new else rule["tier"]

    # The table widgets below let a viewer hide a column via their own
    # header menu, with no built-in way to bring it back -- bumping this
    # generation number changes the widgets' key, forcing Streamlit to
    # remount them from scratch (any hidden-column state was only ever held
    # client-side by the old instance) as an escape hatch for that. Also
    # what makes the row-count resize below actually take effect: Streamlit
    # widget state persists across reruns keyed by `key`, so just changing
    # a data_editor's seed `data` argument on a later rerun has no effect
    # unless its key also changes. Has to live outside the form below
    # since a plain st.button/number_input (unlike form_submit_button)
    # can't be placed inside one.
    generation_key = f"_editor_generation_{layer['id']}_{key_suffix}"
    generation = st.session_state.get(generation_key, 0)
    if st.button(
        "↺ Reset table view",
        key=f"reset_tables_{layer['id']}_{key_suffix}",
        help="If a column got hidden via the table's own menu and won't come back, this brings it back.",
    ):
        st.session_state[generation_key] = generation + 1
        st.rerun()

    # A resize-by-count control instead of requiring N clicks on the
    # table's own "+" affordance to reach position N (e.g. constraining
    # argument 5 used to mean adding 5 rows one at a time) -- changing this
    # pads with blank/unconstrained rows or truncates, then forces a
    # remount (see generation comment above) so the new row count actually
    # shows up. Note: since this necessarily remounts the table, it
    # replays from the last *saved* values, not any not-yet-saved in-grid
    # edits made since -- a real but minor rough edge of Streamlit's
    # widget-state model.
    working_key = f"_positional_working_{layer['id']}_{key_suffix}"
    working_rows = st.session_state.get(working_key, saved_positional_rows)
    count_key = f"_positional_row_count_{layer['id']}_{key_suffix}"
    if count_key not in st.session_state:
        st.session_state[count_key] = len(working_rows)

    def _resize_positional_rows():
        target = st.session_state[count_key]
        current = st.session_state.get(working_key, saved_positional_rows)
        if target > len(current):
            current = current + [dict(blank_positional_row) for _ in range(target - len(current))]
        else:
            current = current[:target]
        st.session_state[working_key] = current
        st.session_state[generation_key] = st.session_state.get(generation_key, 0) + 1

    st.number_input(
        "Number of positional argument rows (row 0 = binary, optional like every other position)",
        min_value=0,
        step=1,
        key=count_key,
        on_change=_resize_positional_rows,
    )
    working_rows = st.session_state.get(working_key, saved_positional_rows)

    with st.form(f"rule_form_{layer['id']}_{key_suffix}"):
        st.caption(
            "Positional constraints -- the row number IS the argv position (row 0 is the binary "
            'itself). A new row starts with whitelist blank and blacklist pre-filled with "'
            + BLACKLIST_MATCHES_NOTHING
            + '" -- leave both as they are, or leave the row out entirely, to leave that position '
            "unconstrained. Both fields are used exactly as typed, so clearing the blacklist to "
            "truly empty makes it match (and reject) every value, not \"no blacklist\"."
        )
        # A plain list of row dicts renders with no columns at all once it's
        # empty (Streamlit can't infer a schema from zero rows) -- same
        # issue the options table used to hit. An explicitly-columned empty
        # DataFrame keeps the headers (and the "+" add-row affordance)
        # showing even at a genuine 0-row count.
        #
        # "position" is a real (disabled) column, not the pandas index --
        # the index's own displayed value for a row added via the table's
        # native "+" affordance isn't configurable the way a column's is
        # (see the TextColumn `default` comment below), so it's shown as
        # literal "None" the same way an un-configured text cell was.
        # Recomputed fresh from row order on every render; there's no way
        # to reorder rows within this table (only the resize control and
        # deleting change row count/order), so it's always accurate except
        # transiently for a row just added via the native "+", until the
        # next full reseed (Save, or the resize control above).
        positional_seed = [dict(row, position=i) for i, row in enumerate(working_rows)]
        positional_edited = st.data_editor(
            pd.DataFrame(positional_seed, columns=["position", "whitelist", "blacklist"]),
            num_rows="dynamic",
            key=f"positional_editor_{layer['id']}_{key_suffix}_{generation}",
            column_config={
                "position": st.column_config.NumberColumn("Position", disabled=True, default=0),
                # `default` is what a row added via the table's own native
                # "+" affordance gets for this column -- undocumented
                # anywhere obvious, but confirmed directly: it's None
                # otherwise, which renders as the literal text "None"
                # rather than a blank, editable cell. Setting it here to
                # the exact same value an unedited row is seeded with
                # elsewhere (_positional_rows/blank_positional_row) means
                # a native-added row looks identical to one added via the
                # resize control above.
                "whitelist": st.column_config.TextColumn(
                    "Whitelist (regex, or {roots})",
                    help='Use the literal "{roots}" for "inside an addressable directory".',
                    default="",
                ),
                "blacklist": st.column_config.TextColumn("Blacklist (regex)", default=BLACKLIST_MATCHES_NOTHING),
            },
            column_order=["position", "whitelist", "blacklist"],
            hide_index=True,
        )
        st.caption(
            "If an argument is a filesystem path, prefer a whitelist -- ideally {roots} -- over a "
            "blacklist alone; a blacklist can be bypassed via symlinks, \"..\", or alternate path spellings."
        )

        st.caption(
            "Options -- at least one of short/long is required per row. Including a row always "
            "requires that option to be present; leave whitelist blank and blacklist as its "
            "pre-filled default to accept any (or no) value, or enter \"^$\" as the whitelist to "
            "require it be present with NO value (an absent value is matched as the empty string)."
        )
        option_edited = st.data_editor(
            pd.DataFrame(saved_option_rows, columns=["short", "long", "whitelist", "blacklist"]),
            num_rows="dynamic",
            key=f"option_editor_{layer['id']}_{key_suffix}_{generation}",
            column_config={
                # default="" on every text column here too -- see the
                # positional table's own comment above for why (otherwise
                # a row added via the table's native "+" shows literal
                # "None" in every cell instead of a blank, editable one).
                "short": st.column_config.TextColumn("Short (-f)", default=""),
                "long": st.column_config.TextColumn("Long (--force)", default=""),
                "whitelist": st.column_config.TextColumn("Whitelist (regex)", default=""),
                "blacklist": st.column_config.TextColumn("Blacklist (regex)", default=BLACKLIST_MATCHES_NOTHING),
            },
            column_order=["short", "long", "whitelist", "blacklist"],
            hide_index=True,
        )

        tier = st.radio(
            "Tier",
            options=TIER_OPTIONS,
            index=TIER_OPTIONS.index(tier_default),
            format_func=lambda t: TIER_LABELS[t],
            horizontal=True,
            key=f"tier_editor_{layer['id']}_{key_suffix}",
        )

        save_col, cancel_col = st.columns(2)
        submitted = save_col.form_submit_button("Save rule", type="primary")
        cancelled = cancel_col.form_submit_button("Cancel")

    def _clear_editor_state():
        st.session_state.pop(f"_editing_rule_{layer['id']}_{key_suffix}", None)
        st.session_state.pop(working_key, None)
        st.session_state.pop(count_key, None)
        st.session_state.pop(generation_key, None)

    if cancelled:
        _clear_editor_state()
        st.rerun()

    if submitted:
        # data_editor returns the same type it was given -- a DataFrame in,
        # a DataFrame out -- so convert back to plain dicts before building.
        positional_constraints, errors = _build_positional_constraints(positional_edited.to_dict("records"))
        option_constraints, option_errors = _build_option_constraints(option_edited.to_dict("records"))
        errors += option_errors
        if _uses_roots(positional_constraints, option_constraints) and tier == "allow":
            errors.append(
                'A rule using "{roots}" cannot have tier "allow" -- only "ask" or "deny" '
                "(only the daemon can authoritatively check directory containment)."
            )
        if errors:
            for e in errors:
                st.error(e)
        else:
            if is_new:
                result = create_policy_layer_rule(
                    AUTH_DOMAIN, TOKEN, layer["id"], positional_constraints, option_constraints, tier
                )
            else:
                result = update_policy_layer_rule(
                    AUTH_DOMAIN,
                    TOKEN,
                    layer["id"],
                    rule["id"],
                    positional_constraints=positional_constraints,
                    option_constraints=option_constraints,
                    tier=tier,
                )
            if result and result.get("error"):
                st.error(result["error"])
            else:
                _clear_editor_state()
                st.session_state.pop("_adding_rule_" + str(layer["id"]), None)
                _refresh()
                st.rerun()


st.header("Create a Policy Layer")
with st.form("create_policy_layer_form", clear_on_submit=True):
    name = st.text_input("Name", placeholder="npm scripts")
    if st.form_submit_button("Create"):
        if not name.strip():
            st.error("Name is required.")
        else:
            result = create_policy_layer(AUTH_DOMAIN, TOKEN, name.strip())
            if result and result.get("error"):
                st.error(result["error"])
            else:
                _refresh()
                st.rerun()

st.divider()
st.header("Policy Layers")
if not policy_layers:
    st.caption("No Policy Layers yet.")
for layer in policy_layers:
    with st.container(border=True):
        top = st.columns([3, 2, 1])
        with top[0]:
            new_name = st.text_input(
                "Rename", value=layer["name"], key=f"layer_name_{layer['id']}", label_visibility="collapsed"
            )
            if new_name.strip() and new_name != layer["name"]:
                if st.button("Save name", key=f"layer_save_{layer['id']}"):
                    result = rename_policy_layer(AUTH_DOMAIN, TOKEN, layer["id"], new_name.strip())
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        with top[2]:
            confirm_key = f"confirm_delete_layer_{layer['id']}"
            if st.session_state.get(confirm_key):
                if st.button("Confirm delete", key=f"do_delete_layer_{layer['id']}", type="primary"):
                    result = delete_policy_layer(AUTH_DOMAIN, TOKEN, layer["id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.session_state.pop(confirm_key, None)
                        st.rerun()
                if st.button("Cancel", key=f"cancel_delete_layer_{layer['id']}"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
            else:
                if st.button("Delete", key=f"delete_layer_{layer['id']}"):
                    st.session_state[confirm_key] = True
                    st.rerun()

        if hosts:
            st.caption("Enabled on:")
            checkbox_cols = st.columns(min(len(hosts), 3) or 1)
            for i, host in enumerate(hosts):
                checked = host["host_id"] in layer["host_ids"]
                with checkbox_cols[i % len(checkbox_cols)]:
                    new_checked = st.checkbox(
                        host["label"], value=checked, key=f"layer_{layer['id']}_host_{host['host_id']}"
                    )
                if new_checked != checked:
                    if new_checked:
                        result = add_policy_layer_to_host(AUTH_DOMAIN, TOKEN, layer["id"], host["host_id"])
                    else:
                        result = remove_policy_layer_from_host(AUTH_DOMAIN, TOKEN, layer["id"], host["host_id"])
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
        else:
            st.caption("No hosts to enable this on yet.")

        st.markdown("**Rules** (checked in order, first match wins; no match is denied)")
        rules = layer["rules"]
        if not rules:
            st.caption("No rules yet -- this layer denies everything until you add one.")
        for idx, rule in enumerate(rules):
            editing_key = f"_editing_rule_{layer['id']}_{rule['id']}"
            row = st.columns([0.6, 0.6, 5, 1, 1])
            with row[0]:
                if idx > 0 and st.button("▲", key=f"up_{rule['id']}"):
                    ids = [r["id"] for r in rules]
                    ids[idx - 1], ids[idx] = ids[idx], ids[idx - 1]
                    result = reorder_policy_layer_rules(AUTH_DOMAIN, TOKEN, layer["id"], ids)
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
            with row[1]:
                if idx < len(rules) - 1 and st.button("▼", key=f"down_{rule['id']}"):
                    ids = [r["id"] for r in rules]
                    ids[idx + 1], ids[idx] = ids[idx], ids[idx + 1]
                    result = reorder_policy_layer_rules(AUTH_DOMAIN, TOKEN, layer["id"], ids)
                    if result and result.get("error"):
                        st.error(result["error"])
                    else:
                        _refresh()
                        st.rerun()
            with row[2]:
                # st.text, not st.caption/st.markdown -- describe_rule()
                # embeds raw regex source (arbitrary "$", "^", "*", "_",
                # backticks...), and Streamlit's markdown renderer treats
                # "$...$" as inline LaTeX math, which mangled patterns like
                # "^npm$" into garbled math notation when this used
                # st.caption. st.text does no Markdown/HTML parsing at all.
                st.text(describe_rule(rule))
            with row[3]:
                if st.button("Edit", key=f"edit_{rule['id']}"):
                    st.session_state[editing_key] = not st.session_state.get(editing_key, False)
                    st.rerun()
            with row[4]:
                rule_confirm_key = f"confirm_delete_rule_{rule['id']}"
                if st.session_state.get(rule_confirm_key):
                    if st.button("Confirm", key=f"do_delete_rule_{rule['id']}", type="primary"):
                        result = delete_policy_layer_rule(AUTH_DOMAIN, TOKEN, layer["id"], rule["id"])
                        if result and result.get("error"):
                            st.error(result["error"])
                        else:
                            _refresh()
                            st.session_state.pop(rule_confirm_key, None)
                            st.rerun()
                else:
                    if st.button("Delete", key=f"delete_rule_{rule['id']}"):
                        st.session_state[rule_confirm_key] = True
                        st.rerun()
            if st.session_state.get(editing_key):
                _render_rule_editor(layer, rule)

        adding_key = f"_adding_rule_{layer['id']}"
        if st.session_state.get(adding_key):
            _render_rule_editor(layer, None)
        elif st.button("+ Add rule", key=f"add_rule_{layer['id']}"):
            st.session_state[adding_key] = True
            st.rerun()

# A fixed-height slot for the save status message -- see
# pages/settings_profile.py's own copy of this for why (reserved whether or
# not anything is actually shown in it this rerun, so "← Back to chat"
# below doesn't jump up/down depending on whether a save just happened).
st.html("<style>.st-key-save_status_row { min-height: 3rem; }</style>")
with st.container(key="save_status_row"):
    if st.session_state.pop("_just_saved", False):
        st.success("Update saved")

st.divider()
st.button("← Back to chat", on_click=lambda: st.switch_page("pages/chat.py"))
