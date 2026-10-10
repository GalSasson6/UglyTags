ACTION_ROW = 1
BUTTON = 2
SELECT_MENU = 3
SECTION = 9
TEXT_DISPLAY = 10
THUMBNAIL = 11
MEDIA_GALLERY = 12
FILE = 13
SEPARATOR = 14
CONTAINER = 17
V2_TYPES = {SECTION, TEXT_DISPLAY, THUMBNAIL, MEDIA_GALLERY, FILE, SEPARATOR, CONTAINER}
MAX_V2_COMPONENTS = 40
BUTTON_STYLES = {1, 2, 3, 4, 5}
LINK_STYLE = 5


class ComponentError(Exception):
    pass


def _media(data, message: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"]:
        raise ComponentError(message)
    return {"url": data["url"]}


def _emoji(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    emoji = {key: data[key] for key in ("id", "name", "animated") if key in data}
    return emoji if emoji.get("id") or emoji.get("name") else None


def parse_component(data, parent: int | None = None, parameter: str | None = None) -> dict:
    if not isinstance(data, dict):
        raise ComponentError("Components must be objects")
    kind = data.get("type")
    if kind == ACTION_ROW:
        if parent not in (None, CONTAINER):
            raise ComponentError("Action Rows must be a Parent Component or inside of Containers")
        children = data.get("components")
        if not isinstance(children, list) or not children:
            raise ComponentError("Action Rows must contain at least 1 component")
        if len(children) > 5:
            raise ComponentError("Action Rows must contain at most 5 component")
        return {
            "type": ACTION_ROW,
            "components": [parse_component(x, ACTION_ROW) for x in children],
        }
    if kind == BUTTON:
        if parent is not None and parent != ACTION_ROW and parameter != "accessory":
            raise ComponentError("Buttons must only be in Action Rows or Section Accessories")
        button = {"type": BUTTON, "style": 1}
        style = data.get("style")
        if isinstance(style, int) and not isinstance(style, bool) and style in BUTTON_STYLES:
            button["style"] = style
        if "disabled" in data:
            button["disabled"] = bool(data["disabled"])
        if isinstance(data.get("url"), str):
            button["url"] = data["url"][:512]
            button["style"] = LINK_STYLE
        elif isinstance(data.get("label"), str):
            button["label"] = data["label"][:80]
            if button["style"] == LINK_STYLE:
                button["style"] = 1
        if emoji := _emoji(data.get("emoji")):
            button["emoji"] = emoji
        if isinstance(data.get("run"), str):
            button["run"] = data["run"]
        if not button.get("url") and not button.get("label"):
            raise ComponentError("Buttons must contain a url or a label")
        return button
    if kind == CONTAINER:
        if parent is not None:
            raise ComponentError(
                "Containers must be a Parent Component, not inside of any components"
            )
        container = {"type": CONTAINER, "components": []}
        color = data.get("accent_color")
        if isinstance(color, str):
            try:
                container["accent_color"] = int(color.replace("#", ""), 16)
            except ValueError:
                container["accent_color"] = 0
        elif isinstance(color, int | float) and not isinstance(color, bool):
            container["accent_color"] = max(0, min(0xFFFFFF, int(color)))
        elif isinstance(color, list) and len(color) == 3:
            try:
                red, green, blue = (max(0, min(255, int(x))) for x in color)
                container["accent_color"] = (red << 16) | (green << 8) | blue
            except (TypeError, ValueError):
                pass
        if "spoiler" in data:
            container["spoiler"] = bool(data["spoiler"])
        children = data.get("components")
        if not isinstance(children, list) or not children:
            raise ComponentError("Containers must contain at least 1 component")
        if len(children) > 40:
            raise ComponentError("Containers must contain at most 40 components")
        container["components"] = [parse_component(x, CONTAINER) for x in children]
        return container
    if kind == FILE:
        if parent not in (None, CONTAINER):
            raise ComponentError("Files must be a Parent Component or inside of Containers")
        media = _media(data.get("file"), "File must contain a Media Object")
        if not media["url"].startswith("attachment://"):
            raise ComponentError("File Media Object url must be an attachment:// reference")
        file = {"type": FILE, "file": media}
        if "spoiler" in data:
            file["spoiler"] = bool(data["spoiler"])
        return file
    if kind == MEDIA_GALLERY:
        if parent not in (None, CONTAINER):
            raise ComponentError("Media Gallery must be a Parent Component or inside of Containers")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise ComponentError("Media Galleries must contain at least 1 item")
        if len(items) > 10:
            raise ComponentError("Media Galleries must contain at most 10 items")
        gallery = {"type": MEDIA_GALLERY, "items": []}
        for item in items:
            if not isinstance(item, dict):
                raise ComponentError("Media Gallery items must be objects")
            parsed = {
                "media": _media(
                    item.get("media"), "Media Gallery items must contain a Media Object"
                )
            }
            if "description" in item:
                parsed["description"] = str(item["description"])[:1024]
            if "spoiler" in item:
                parsed["spoiler"] = bool(item["spoiler"])
            gallery["items"].append(parsed)
        return gallery
    if kind == SECTION:
        if parent not in (None, CONTAINER):
            raise ComponentError("Sections must be a Parent Component or inside of Containers")
        children = data.get("components")
        if not isinstance(children, list) or not children:
            raise ComponentError("Sections must contain at least 1 component")
        if len(children) > 3:
            raise ComponentError("Sections must contain at most 3 components")
        if not isinstance(data.get("accessory"), dict):
            raise ComponentError("Sections must contain a valid Accessory Component")
        return {
            "type": SECTION,
            "components": [parse_component(x, SECTION, "components") for x in children],
            "accessory": parse_component(data["accessory"], SECTION, "accessory"),
        }
    if kind == SELECT_MENU:
        if parent not in (None, ACTION_ROW):
            raise ComponentError("Select Menus must only be in Action Rows")
        menu = {"type": SELECT_MENU, "options": []}
        if isinstance(data.get("placeholder"), str):
            menu["placeholder"] = data["placeholder"][:150]
        for key in ("max_values", "min_values"):
            value = data.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                menu[key] = max(1, min(25, int(value)))
        if isinstance(data.get("run"), str):
            menu["run"] = data["run"]
        options = data.get("options")
        if not isinstance(options, list) or not options:
            raise ComponentError("Select Menu options must contain at least 1 option")
        if len(options) > 25:
            raise ComponentError("Select Menu options must contain at most 25 options")
        for option in options:
            if not isinstance(option, dict):
                raise ComponentError("Select Menu options need to be objects")
            parsed = {
                key: option[key][:100]
                for key in ("label", "value", "description")
                if isinstance(option.get(key), str) and option[key]
            }
            if emoji := _emoji(option.get("emoji")):
                parsed["emoji"] = emoji
            if "default" in option:
                parsed["default"] = bool(option["default"])
            if not parsed.get("label") or not parsed.get("value"):
                raise ComponentError("Select Menu options must contain both a label and a value")
            menu["options"].append(parsed)
        return menu
    if kind == SEPARATOR:
        if parent not in (None, CONTAINER):
            raise ComponentError("Separators must be a Parent Component or inside of Containers")
        separator = {"type": SEPARATOR}
        if "divider" in data:
            separator["divider"] = bool(data["divider"])
        if data.get("spacing") in (1, 2):
            separator["spacing"] = data["spacing"]
        return separator
    if kind == TEXT_DISPLAY:
        if parent is not None and parent != CONTAINER and parameter != "components":
            raise ComponentError(
                "Text Displays must be a Parent Component or inside of Containers/Sections"
            )
        return {"type": TEXT_DISPLAY, "content": str(data.get("content"))[:4000]}
    if kind == THUMBNAIL:
        if parent != SECTION or parameter != "accessory":
            raise ComponentError("Thumbnails must only be in Section Accessories")
        thumbnail = {
            "type": THUMBNAIL,
            "media": _media(data.get("media"), "Thumbnails must contain a Media Object"),
        }
        if "description" in data:
            thumbnail["description"] = str(data["description"])[:1024]
        if "spoiler" in data:
            thumbnail["spoiler"] = bool(data["spoiler"])
        return thumbnail
    raise ComponentError("TagScript does not support this kind of component yet")


def count_components(components: list[dict]) -> int:
    total = 0
    for component in components:
        total += 1 + count_components(component.get("components", []))
        if "accessory" in component:
            total += 1
    return total


def check_limit(components: list[dict]):
    rows = sum(component["type"] == ACTION_ROW for component in components)
    buttons = sum(component["type"] == BUTTON for component in components)
    if rows and buttons:
        raise ComponentError("Only support ActionRows or Buttons, not both for now")
    if rows > 5:
        raise ComponentError("TagScript only supports up to 5 action rows")
    if buttons > 25:
        raise ComponentError("TagScript only supports up to 25 buttons")
    if is_v2(components) and count_components(components) > MAX_V2_COMPONENTS:
        raise ComponentError(f"Messages can contain at most {MAX_V2_COMPONENTS} components")


def is_v2(components: list[dict]) -> bool:
    return any(component["type"] in V2_TYPES for component in components)
