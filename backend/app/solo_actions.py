from __future__ import annotations

VISUAL_SOLO_ACTIONS = {
    "finish",
    "wait",
    "screenshot",
    "click",
    "double_click",
    "right_click",
    "move_mouse",
    "scroll",
    "type_text",
    "press_keys",
    "execute_command",
    "open_url",
}

DEFAULT_TOOL_SOLO_ACTIONS = {
    "web_search",
    "get_current_time",
    "get_file_info",
    "list_directory",
    "read_text_file",
    "search_files",
    "search_text",
}

CONFIGURED_CAPABILITY_SOLO_ACTIONS = {
    "run_configured_tool",
    "call_mcp_tool",
}

ALLOWED_SOLO_ACTIONS = (
    VISUAL_SOLO_ACTIONS
    | DEFAULT_TOOL_SOLO_ACTIONS
    | CONFIGURED_CAPABILITY_SOLO_ACTIONS
)

BATCH_EXECUTABLE_ACTIONS = {
    "click",
    "double_click",
    "scroll",
    "type_text",
    "press_keys",
    "wait",
}

SAFE_DEFAULT_TOOL_SOLO_ACTIONS = DEFAULT_TOOL_SOLO_ACTIONS


def allowed_actions_text(include_configured: bool = True) -> str:
    groups = [
        "finish | wait | screenshot | click | double_click | right_click",
        "move_mouse | scroll | type_text | press_keys | execute_command | open_url",
        "web_search | get_current_time | get_file_info | list_directory",
        "read_text_file | search_files | search_text",
    ]
    if include_configured:
        groups.append("run_configured_tool | call_mcp_tool")
    return " |\n  ".join(groups)
