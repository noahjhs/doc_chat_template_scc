def autofocus_input_js(aria_label):
    """JS that finds the first <input> with the given aria-label (the
    label text passed to st.text_input, e.g. "Username") in the *parent*
    document -- see click_anchor_js's docstring for why this always
    targets window.parent, not the local iframe -- and focuses it, so a
    signin/signup form is ready to type into without an extra click.
    Polls briefly rather than running once: Streamlit renders the actual
    input asynchronously, so it may not exist in the DOM yet the instant
    this script first runs. Gives up quietly after ~2s (e.g. if the label
    text ever stops matching) rather than polling forever."""
    return f"""
    (function() {{
        var tries = 0;
        var interval = setInterval(function() {{
            var el = window.parent.document.querySelector('input[aria-label="{aria_label}"]');
            if (el) {{
                el.focus();
                clearInterval(interval);
            }} else if (++tries > 40) {{
                clearInterval(interval);
            }}
        }}, 50);
    }})();
    """


def click_anchor_js(url_js_expr):
    """JS statements that create a real anchor in the *parent* document
    (window.parent.document) pointing at `url_js_expr` -- a JS expression
    evaluating to the target URL: json.dumps() a plain string for a URL
    known at render time, or a variable/property name for one only
    discovered later (e.g. from a fetch() response) -- and click it.

    st.iframe renders into a sandboxed iframe that can read/write a
    same-origin parent's DOM, but is separately blocked from directly
    assigning that parent's window.location -- the assignment just
    silently no-ops, which is why a plain `window.parent.location.href =
    ...` doesn't reliably navigate anywhere. Clicking a genuine link that
    lives in the parent's own document isn't subject to that restriction,
    so this is what actually works."""
    return (
        "var a = window.parent.document.createElement('a');"
        f"a.href = {url_js_expr};"
        "a.target = '_self';"
        "window.parent.document.body.appendChild(a);"
        "a.click();"
    )
