"""Theme + light/dark mode, stored server-side (meta table) so every device gets the same look."""
from .db import connect, get_meta, set_meta

THEMES = {
    "notebook": {"name": "Notebook", "blurb": "Google Sans, soft blue panels, pill buttons. Modeled on Gemini Notebook.",
                 "ui": '"Google Sans Flex", sans-serif', "heading": '"Google Sans Flex", sans-serif', "fonts": "Google Sans Flex · Google Sans Code",
                 "light": ["#ffffff", "#f3f6fb", "#3186ff", "#1f1f1f"], "dark": ["#131314", "#1e1f20", "#a8c7fa", "#e3e3e3"],
                 "btn": "#1f1f1f", "btn_ink": "#ffffff", "radius": "999px"},
    "sage": {"name": "Sage", "blurb": "The first look: system font, calm green, tidy cards.",
             "ui": 'system-ui, sans-serif', "heading": 'system-ui, sans-serif', "fonts": "System UI",
             "light": ["#f6f5f1", "#ffffff", "#1f6f5c", "#1d1f21"], "dark": ["#121416", "#1b1e21", "#4fb89b", "#e6e6e3"],
             "btn": "#1f6f5c", "btn_ink": "#ffffff", "radius": "8px"},
    "paper": {"name": "Paper", "blurb": "Serif notes for long reading, warm cream, ink-blue accents.",
              "ui": '"IBM Plex Sans", sans-serif', "heading": '"Newsreader", Georgia, serif', "fonts": "Newsreader · IBM Plex Sans",
              "light": ["#f7f3ea", "#fffdf8", "#2d4a7a", "#2a2622"], "dark": ["#1a1815", "#22201c", "#9db4de", "#ece6da"],
              "btn": "#2d4a7a", "btn_ink": "#ffffff", "radius": "6px"},
}
MODES = {"auto": "System", "light": "Light", "dark": "Dark"}
DEFAULT = {"theme": "notebook", "mode": "auto"}  # Notebook is the main look; Sage and Paper are alternatives


def get(conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        theme = get_meta(conn, "theme", DEFAULT["theme"])
        mode = get_meta(conn, "mode", DEFAULT["mode"])
    except Exception:  # noqa: BLE001 — before migrations run (first boot) the meta table may not exist
        theme, mode = DEFAULT["theme"], DEFAULT["mode"]
    finally:
        if own:
            conn.close()
    return {"theme": theme if theme in THEMES else DEFAULT["theme"], "mode": mode if mode in MODES else DEFAULT["mode"]}


def save(conn, theme: str, mode: str) -> None:
    if theme not in THEMES or mode not in MODES:
        raise ValueError("unknown theme or mode")
    with conn:
        set_meta(conn, "theme", theme)
        set_meta(conn, "mode", mode)
