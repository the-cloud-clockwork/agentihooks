"""Codex 0.162.1 app-server hooks/list currentHash for each entry of CODEX_HOOKS."""

CODEX_HOOKS = {
    "hooks": {
        "Interrupt": [
            {"hooks": [{"type": "command", "command": "bash /x/herdr-agent-state.sh idle"}]},
            {"matcher": "m", "hooks": [{"type": "command", "command": "c", "timeout": 9, "async": True}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command", "command": "/x/agentihooks-hook.sh"}]},
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash /x/herdr-agent-state.sh idle",
                        "statusMessage": "s",
                        "additionalContextLimit": 7,
                    }
                ]
            },
        ],
        "UserPromptSubmit": [
            {
                "matcher": "zz",
                "hooks": [
                    {"type": "command", "command": "a", "additionalContextLimit": 0},
                    {"type": "command", "command": "b", "additionalContextLimit": 2500, "timeout": 0},
                ],
            }
        ],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "p", "timeout": 30, "statusMessage": "é"}]}
        ],
        "SessionEnd": [
            {"hooks": [{"type": "command", "command": "e", "timeout": 99}]},
            {"hooks": [{"type": "command", "command": "f", "timeout": 0, "statusMessage": ""}]},
        ],
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": "   "},
                    {"type": "command", "command": "q", "commandWindows": "w"},
                ]
            }
        ],
    }
}

CODEX_HOOK_HASHES = {
    "interrupt:0:0": "sha256:9c044226c7ff8c9eb87b42400bb51b9290b7555ebcd43b451745c89694828be7",
    "interrupt:1:0": "sha256:009ac8eea97d0c53bec61fba09e3630ada40d18a180cc2b722854b6ca1237438",
    "pre_tool_use:0:0": "sha256:92efda7cc89cc3510e155836a793b6f8b28789b2aa9e53122961eff3af0a3d24",
    "session_end:0:0": "sha256:1c961b1feb2ddfd034b56b57216af374518582fbf5dc0173dcebc20889d389f5",
    "session_end:1:0": "sha256:0d13177c538df58fc6fb454550ce53df5b4e5b7c4cc8970ebf400e7409b5d3cf",
    "session_start:0:1": "sha256:eac8c1c5a0f1a81cfe6f46337ba676b39a7c96a24a9c302e775f94f21b571891",
    "stop:0:0": "sha256:f8c97e05f9aeec4e4f01985044a5944fc59c699f79942dba159c3d4b36e49696",
    "stop:1:0": "sha256:1eb006a946829f29749b0a44d6191b1b3dd84207487b5fe9ce01d3997fe89398",
    "user_prompt_submit:0:0": "sha256:600a47d46454e38b7b29c06eba837f5c0bf37c36d2b6c9067d639762bdf95008",
    "user_prompt_submit:0:1": "sha256:39b09504f328987a0331ce52f9f3eaf746a9aa99d08f2815db1b3f2fbaa69301",
}
