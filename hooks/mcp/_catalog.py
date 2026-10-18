CATALOG = {
    "channels": [
        {
            "name": "channel_publish",
            "description": "Publish a message to a named broadcast channel.\n"
            "\n"
            "        Subscriptions are operator-configured via the "
            "`AGENTIHOOKS_BASE_CHANNELS`\n"
            "        env var (set in the profile's settings.overrides.json `env` "
            "block, or\n"
            "        per-repo via .claude/settings.local.json, or at container "
            "launch). Other\n"
            "        channel names are accepted but only deliver if a session "
            "has wildcard\n"
            "        subscription.\n"
            "\n"
            "        Args:\n"
            '            channel: Channel name (e.g. "brain", "ops-alerts", '
            '"deploy-status")\n'
            "            message: Message content to broadcast\n"
            '            severity: "info", "alert", or "critical" (default: '
            "info)\n"
            "            ttl_seconds: Time-to-live in seconds (default: 3600)\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status and message_id.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "channel": {"title": "Channel", "type": "string"},
                    "message": {"title": "Message", "type": "string"},
                    "severity": {"default": "info", "title": "Severity", "type": "string"},
                    "ttl_seconds": {"default": 3600, "title": "Ttl Seconds", "type": "integer"},
                },
                "required": ["channel", "message"],
                "title": "channel_publishArguments",
                "type": "object",
            },
        },
        {
            "name": "channel_list",
            "description": "List all channels with active (non-expired) messages.\n"
            "\n"
            "        Returns:\n"
            "            JSON with channels list, message counts per channel, "
            "and global message count.\n"
            "        ",
            "inputSchema": {"properties": {}, "title": "channel_listArguments", "type": "object"},
        },
        {
            "name": "channel_acknowledge",
            "description": "Acknowledge a broadcast message — stops it from re-injecting for "
            "this session.\n"
            "\n"
            "        Use when you've processed a persistent broadcast and don't "
            "need to see it again.\n"
            "        The message remains active for other sessions that haven't "
            "acknowledged it.\n"
            "        The message ID is shown in broadcast banners as 'ID: "
            "<id>'.\n"
            "\n"
            "        Args:\n"
            "            message_id: The broadcast message ID to acknowledge\n"
            "            session_id: THIS session's own id, shown to you at "
            "session start.\n"
            "                The ack is recorded against it, so it must name a "
            "real session\n"
            "                — required under a network MCP transport, which has "
            "no\n"
            "                per-caller environment to fall back on.\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "message_id": {"title": "Message Id", "type": "string"},
                    "session_id": {"default": "", "title": "Session Id", "type": "string"},
                },
                "required": ["message_id"],
                "title": "channel_acknowledgeArguments",
                "type": "object",
            },
        },
        {
            "name": "channel_clear",
            "description": "Clear broadcast messages.\n"
            "\n"
            "        If message_id is provided: clear that specific message.\n"
            "        If channel is provided: clear all messages on that "
            "channel.\n"
            "        If neither: clear all broadcasts.\n"
            "\n"
            "        Args:\n"
            "            channel: Channel name to clear (optional)\n"
            "            message_id: Specific message ID to clear (optional)\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status and count of messages "
            "removed.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "channel": {"default": "", "title": "Channel", "type": "string"},
                    "message_id": {"default": "", "title": "Message Id", "type": "string"},
                },
                "title": "channel_clearArguments",
                "type": "object",
            },
        },
        {
            "name": "brain_refresh",
            "description": "Force the brain adapter to re-read its source and republish to the "
            "brain channel.\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status and whether new content was "
            "published.\n"
            "        ",
            "inputSchema": {"properties": {}, "title": "brain_refreshArguments", "type": "object"},
        },
        {
            "name": "brain_status",
            "description": "Return current brain adapter state: source type, path, entry count, "
            "channel.\n"
            "\n"
            "        Returns:\n"
            "            JSON with brain adapter status details.\n"
            "        ",
            "inputSchema": {"properties": {}, "title": "brain_statusArguments", "type": "object"},
        },
    ],
    "enforcement": [
        {
            "name": "enforcement_set",
            "description": "Register a drumbeat enforcement that re-injects every N tool "
            "calls.\n"
            "\n"
            "        Global by default: every session on the machine sees it. "
            "A rule that\n"
            "        belongs to one repository must use local=True, which "
            "writes\n"
            "        <git-root>/.agentihooks/enforcements.json and reaches "
            "only sessions\n"
            "        inside that repository. Permanent until cleared. No "
            "severity, no TTL — just a recurring reminder. Cheap because\n"
            "        re-injection only adds context tokens, no external API "
            "calls.\n"
            "\n"
            "        Args:\n"
            "            message: Reminder text to inject for type=message.\n"
            "            cadence: Re-inject every N tool calls. Required, "
            "must be >= 1.\n"
            "            tag: Optional tag for grouping (lets you "
            "clear-by-tag later).\n"
            "            matcher: Optional tool matcher; the enforcement is "
            "then delivered only on\n"
            "                matching tool calls and its cadence counts those "
            "calls only.\n"
            "                Grammar: any, bash, edit, mcp, mcp__<server>, "
            "mcp__<server>__<tool>,\n"
            "                bash.<cli> (e.g. bash.kubectl); join "
            'alternatives with "+".\n'
            "            local: Store it in the current repository only (see "
            "above).\n"
            "            cwd: Directory inside the target repository; "
            "defaults to the session's project directory.\n"
            "            type: Enforcement type: message (default) or rule.\n"
            "            path: Absolute UTF-8 rule file path required for "
            "type=rule. Its complete\n"
            "                current contents are read on every injection.\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status and enforcement_id.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "message": {"default": "", "title": "Message", "type": "string"},
                    "cadence": {"default": 5, "title": "Cadence", "type": "integer"},
                    "tag": {"default": "", "title": "Tag", "type": "string"},
                    "matcher": {"default": "", "title": "Matcher", "type": "string"},
                    "local": {"default": False, "title": "Local", "type": "boolean"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                    "type": {"default": "message", "title": "Type", "type": "string"},
                    "path": {"default": "", "title": "Path", "type": "string"},
                },
                "title": "enforcement_setArguments",
                "type": "object",
            },
        },
        {
            "name": "enforcement_list",
            "description": "List active enforcements with their cadence and tag.\n"
            "\n"
            "        Args:\n"
            "            local: List only the current repository's entries.\n"
            "            cwd: Directory inside the target repository; "
            "defaults to the session's project directory.\n"
            "\n"
            "        Returns:\n"
            "            JSON with the enforcement entries and total count.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "local": {"default": False, "title": "Local", "type": "boolean"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                },
                "title": "enforcement_listArguments",
                "type": "object",
            },
        },
        {
            "name": "enforcement_clear",
            "description": "Clear enforcement messages.\n"
            "\n"
            "        If enforcement_id is provided: clear that specific "
            "enforcement.\n"
            "        If tag is provided: clear all enforcements with that "
            "tag.\n"
            "        If neither: clear ALL enforcements.\n"
            "\n"
            "        Args:\n"
            "            enforcement_id: Specific enforcement ID to clear "
            "(optional)\n"
            "            tag: Tag to clear all matching enforcements "
            "(optional)\n"
            "            local: Clear from the current repository's store "
            "instead of the global one.\n"
            "            cwd: Directory inside the target repository; "
            "defaults to the session's project directory.\n"
            "\n"
            "        Returns:\n"
            "            JSON with success status and count of enforcements "
            "removed.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "enforcement_id": {"default": "", "title": "Enforcement Id", "type": "string"},
                    "tag": {"default": "", "title": "Tag", "type": "string"},
                    "local": {"default": False, "title": "Local", "type": "boolean"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                },
                "title": "enforcement_clearArguments",
                "type": "object",
            },
        },
    ],
    "conditions": [
        {
            "name": "condition_set",
            "description": "Create a condition: a script agentihooks runs on every matching "
            "tool call.\n"
            "\n"
            "        Use ONLY when the operator asks to set/add/create a "
            "condition: his own message\n"
            "        this turn, his comment on your ledger task, or the "
            "master's relay onto it.\n"
            "        Never create one on your own initiative; the call is "
            "refused\n"
            "        otherwise. The condition is live from the next matching "
            "tool call.\n"
            "\n"
            "        The script receives the hook payload as JSON on stdin "
            "(tool_name, tool_input,\n"
            "        tool_response on post, session_id, cwd, transcript_path) "
            "plus AH_* env vars.\n"
            "        stdout: nothing, plain text (context for the agent), or "
            "JSON with context,\n"
            "        tool_input (pre: rewrite), tool_output (post: replace), "
            "decision allow|ask|deny,\n"
            "        reason. Exit 2 denies the call (pre) with stderr as the "
            "reason.\n"
            "\n"
            "        Args:\n"
            '            step: "pre" (before the tool runs), "post" (after it '
            "succeeds),\n"
            '                "stop" (when the agent stops; exit 2 or decision '
            "deny keeps it working) or\n"
            '                "start" (at SessionStart; context only, it can '
            "never deny or rewrite).\n"
            '            matcher: "" or any for stop and start; otherwise any, '
            "a tool name (bash, edit, write, read, webfetch), mcp,\n"
            "                mcp__<server>, mcp__<server>__<tool>, bash.<cli> "
            "(e.g. bash.kubectl);\n"
            '                join alternatives with "+".\n'
            '            name: letters, digits and "_" only.\n'
            "            script: the script body.\n"
            "            session_id: your session id (from the SessionStart "
            "banner).\n"
            '            language: "bash", "python" or "filter" (script is the '
            "filter YAML, checked against the\n"
            "                filter schema; written as "
            "<step>-<matcher>-<name>.filter.yaml).\n"
            '            scope: "global" (bundle-wide; ~/.agentihooks when no '
            "bundle is linked),\n"
            '                "profile" (one profile of the chain), or '
            '"directory"\n'
            "                (<git-root>/.agentihooks/conditions of the "
            "current repository).\n"
            '            profile: profile name for scope="profile"; defaults '
            "to the chain's first.\n"
            "            cwd: directory inside the target repository for "
            'scope="directory".\n'
            "            run_async: run detached; its output is ignored.\n"
            "            replace: overwrite an existing condition with the "
            "same file name.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "step": {"title": "Step", "type": "string"},
                    "matcher": {"title": "Matcher", "type": "string"},
                    "name": {"title": "Name", "type": "string"},
                    "script": {"title": "Script", "type": "string"},
                    "session_id": {"title": "Session Id", "type": "string"},
                    "language": {"default": "bash", "title": "Language", "type": "string"},
                    "scope": {"default": "global", "title": "Scope", "type": "string"},
                    "profile": {"default": "", "title": "Profile", "type": "string"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                    "run_async": {"default": False, "title": "Run Async", "type": "boolean"},
                    "replace": {"default": False, "title": "Replace", "type": "boolean"},
                },
                "required": ["step", "matcher", "name", "script", "session_id"],
                "title": "condition_setArguments",
                "type": "object",
            },
        },
        {
            "name": "condition_list",
            "description": "List condition layers (bundle, profiles, runtime, directory), the "
            "active\n"
            "        conditions in execution order, and files whose names do "
            "not parse.\n"
            "\n"
            "        Args:\n"
            "            cwd: directory whose repository's directory layer to "
            "include; defaults\n"
            "                to the session's project directory.\n"
            "        ",
            "inputSchema": {
                "properties": {"cwd": {"default": "", "title": "Cwd", "type": "string"}},
                "title": "condition_listArguments",
                "type": "object",
            },
        },
        {
            "name": "condition_show",
            "description": "Return the script of an active condition by file name (see "
            "condition_list).\n"
            "\n"
            "        Args:\n"
            "            file: the condition's file name, e.g. "
            '"pre-bash.kubectl-readonly.sh".\n'
            "            cwd: as in condition_list.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "file": {"title": "File", "type": "string"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                },
                "required": ["file"],
                "title": "condition_showArguments",
                "type": "object",
            },
        },
        {
            "name": "condition_clear",
            "description": "Delete a condition file. Only when the operator asks to "
            "remove/clear/delete\n"
            "        a condition: his own message this turn, his comment on "
            "your ledger task, or\n"
            "        the master's relay onto it; refused otherwise.\n"
            "\n"
            "        Args:\n"
            "            file: the condition's file name.\n"
            "            session_id: your session id (from the SessionStart "
            "banner).\n"
            '            scope: "global", "profile" or "directory" when the '
            "name exists in several layers.\n"
            "            cwd: as in condition_list.\n"
            "        ",
            "inputSchema": {
                "properties": {
                    "file": {"title": "File", "type": "string"},
                    "session_id": {"title": "Session Id", "type": "string"},
                    "scope": {"default": "", "title": "Scope", "type": "string"},
                    "cwd": {"default": "", "title": "Cwd", "type": "string"},
                },
                "required": ["file", "session_id"],
                "title": "condition_clearArguments",
                "type": "object",
            },
        },
    ],
}

CAPABILITIES = {
    "experimental": {},
    "prompts": {"listChanged": False},
    "resources": {"subscribe": False, "listChanged": False},
    "tools": {"listChanged": False},
}

PROTOCOL_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"]
