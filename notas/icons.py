"""Inline SVG icons (stroke style), rendered with the ``icon()`` template helper."""
# ruff: noqa: E501  (SVG path data)

from markupsafe import Markup

PATHS = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9.5"/><path d="M10 21v-6h4v6"/>',
    "checks": '<rect x="3" y="3" width="18" height="18" rx="5"/><path d="m8 12.2 2.8 2.8L16.5 9"/>',
    "folder": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.6l2.2 2.2h7.2A2.5 2.5 0 0 1 21 9.7v7.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
    "calendar": '<rect x="3" y="4.5" width="18" height="16.5" rx="3.5"/><path d="M3 9.5h18M8 2.8v3.4M16 2.8v3.4"/>',
    "wallet": '<path d="M18 7V5.8A1.8 1.8 0 0 0 16.2 4H5.5a2.5 2.5 0 0 0 0 5H19a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5A2.5 2.5 0 0 1 3 17.5V6.5"/><path d="M16.5 14.5h.01"/>',
    "key": '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.3-9.3M16.5 6.5l3 3M14 9l2 2"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10.5" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "star": '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.8l-5.2 2.8 1-5.8-4.3-4.1 5.9-.9z"/>',
    "x": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "up": '<path d="m6.5 14.5 5.5-5.5 5.5 5.5"/>',
    "down": '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5.5 15H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v.5"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
    "swap": '<path d="M4 8.5h15l-4-4M20 15.5H5l4 4"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "upload": '<path d="M12 15V8m0 0-3 3m3-3 3 3"/><path d="M20 16.5A4.5 4.5 0 0 0 17.4 8 6 6 0 0 0 6 9.4a4 4 0 0 0 .5 8"/>',
    "edit": '<path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17z"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5L18 7M9 7V4.5h6V7"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "tag": '<path d="M3 12.6V4.5A1.5 1.5 0 0 1 4.5 3h8.1l8.4 8.4a2 2 0 0 1 0 2.8l-6.6 6.6a2 2 0 0 1-2.8 0z"/><path d="M8 8h.01"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M4 4v4h4M20 20v-4h-4"/>',
    "arrow-left": '<path d="M19 12H5m0 0 6-6m-6 6 6 6"/>',
    "arrow-right": '<path d="M5 12h14m0 0-6-6m6 6-6 6"/>',
    "dice": '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M8.5 8.5h.01M15.5 8.5h.01M12 12h.01M8.5 15.5h.01M15.5 15.5h.01"/>',
    "logo": '<rect x="4" y="3" width="16" height="18" rx="4"/><path d="M8.5 8h7M8.5 12h7M8.5 16h4"/>',
}


def icon(name: str, cls: str = "") -> Markup:
    path = PATHS.get(name, "")
    return Markup(  # noqa: S704 - trusted constant markup
        f'<svg class="i {cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{path}</svg>'
    )
