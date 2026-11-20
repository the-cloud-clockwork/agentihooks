"""Credential-read guard behaviour, ported with the guard itself.

These cases came from the bundle hook's own selftest, which ran only on Claude.
The guard now runs on every target, so they run in CI instead of by hand.
"""

import json
from pathlib import Path

import pytest

from hooks.context.credential_guard import (
    ENV_NAMES_SAFE,
    GREP_EXCLUDES,
    KIND_ENVIRONMENT,
    KIND_RECURSIVE,
    RG_EXCLUDES,
    block,
    decide,
    evaluate,
    strip_heredocs,
)

pytestmark = pytest.mark.unit

CASES = [
    # (label, payload, should_block)
    ("read dotenv", {"tool_name": "Read", "tool_input": {"file_path": "/srv/app/.env"}}, True),
    ("read .env.production", {"tool_name": "Read", "tool_input": {"file_path": ".env.production"}}, True),
    ("read stack env", {"tool_name": "Read", "tool_input": {"file_path": "runtime/tracker.env"}}, True),
    ("read .env.example", {"tool_name": "Read", "tool_input": {"file_path": ".env.example"}}, False),
    ("read bashrc", {"tool_name": "Read", "tool_input": {"file_path": "~/.bashrc"}}, True),
    ("read netrc", {"tool_name": "Read", "tool_input": {"file_path": "/home/x/.netrc"}}, True),
    ("read aws creds", {"tool_name": "Read", "tool_input": {"file_path": "~/.aws/credentials"}}, True),
    (
        "read worker grant",
        {"tool_name": "Read", "tool_input": {"file_path": "/home/worker/attempts/exe-1/run/launch-grant"}},
        True,
    ),
    ("grep in worker grant", {"tool_name": "Grep", "tool_input": {"pattern": "v2", "path": "run/launch-grant"}}, True),
    ("cat worker grant", {"tool_name": "Bash", "tool_input": {"command": "cat run/launch-grant"}}, True),
    ("grep worker grant", {"tool_name": "Bash", "tool_input": {"command": "grep v2 ./launch-grant"}}, True),
    (
        "python reads worker grant",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('run/launch-grant').read())\""}},
        True,
    ),
    ("cat glob of worker grant", {"tool_name": "Bash", "tool_input": {"command": "cat run/launch-*"}}, True),
    ("ls worker grant", {"tool_name": "Bash", "tool_input": {"command": "ls -la run/launch-grant"}}, False),
    ("read launch module", {"tool_name": "Read", "tool_input": {"file_path": "scripts/swarm_v2/launch.py"}}, False),
    ("read source file", {"tool_name": "Read", "tool_input": {"file_path": "hooks/secrets.py"}}, False),
    ("grep in dotenv", {"tool_name": "Grep", "tool_input": {"pattern": "URL", "path": ".env"}}, True),
    ("grep in src", {"tool_name": "Grep", "tool_input": {"pattern": "URL", "path": "src/"}}, False),
    ("glob untouched", {"tool_name": "Glob", "tool_input": {"pattern": "**/.env"}}, False),
    ("write untouched", {"tool_name": "Write", "tool_input": {"file_path": ".env"}}, False),
    ("cat dotenv", {"tool_name": "Bash", "tool_input": {"command": "cat .env"}}, True),
    ("source bashrc", {"tool_name": "Bash", "tool_input": {"command": "source ~/.bashrc"}}, True),
    ("dot-source dotenv", {"tool_name": "Bash", "tool_input": {"command": ". ./.env"}}, True),
    ("cat example", {"tool_name": "Bash", "tool_input": {"command": "cat .env.example"}}, False),
    ("ls dotenv", {"tool_name": "Bash", "tool_input": {"command": "ls -la .env"}}, False),
    ("wc dotenv", {"tool_name": "Bash", "tool_input": {"command": "wc -l ~/.bashrc"}}, False),
    ("test -f dotenv", {"tool_name": "Bash", "tool_input": {"command": "test -f .env && echo yes"}}, False),
    ("grep -c bashrc", {"tool_name": "Bash", "tool_input": {"command": "grep -c export ~/.bashrc"}}, False),
    ("grep content bashrc", {"tool_name": "Bash", "tool_input": {"command": "grep export ~/.bashrc"}}, True),
    ("bare env", {"tool_name": "Bash", "tool_input": {"command": "env"}}, True),
    ("bare printenv", {"tool_name": "Bash", "tool_input": {"command": "printenv"}}, True),
    ("export -p", {"tool_name": "Bash", "tool_input": {"command": "export -p"}}, True),
    ("env names", {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1"}}, True),
    ("env count", {"tool_name": "Bash", "tool_input": {"command": "env | wc -l"}}, False),
    ("env grep value", {"tool_name": "Bash", "tool_input": {"command": "env | grep REDIS"}}, True),
    ("env grep count", {"tool_name": "Bash", "tool_input": {"command": "env | grep -c REDIS"}}, False),
    ("env grep color", {"tool_name": "Bash", "tool_input": {"command": "env | grep --color=never ."}}, True),
    ("env grep long option", {"tool_name": "Bash", "tool_input": {"command": "env | grep --line-buffered ."}}, True),
    ("env grep pattern c", {"tool_name": "Bash", "tool_input": {"command": "env | grep -ec ."}}, True),
    ("env grep list", {"tool_name": "Bash", "tool_input": {"command": "env | grep -l ."}}, True),
    ("env wc names from input", {"tool_name": "Bash", "tool_input": {"command": "env | wc --files0-from=-"}}, True),
    ("env names then head", {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | head -3"}}, True),
    ("env sorted names", {"tool_name": "Bash", "tool_input": {"command": "env | sort | cut -d= -f1"}}, True),
    ("env awk names", {"tool_name": "Bash", "tool_input": {"command": "env | awk -F= '{print $1}'"}}, True),
    ("printenv names", {"tool_name": "Bash", "tool_input": {"command": "printenv | sed 's/=.*//'"}}, True),
    ("set names", {"tool_name": "Bash", "tool_input": {"command": "set | cut -d= -f1"}}, True),
    ("export -p names", {"tool_name": "Bash", "tool_input": {"command": "export -p | cut -d= -f1"}}, True),
    ("declare -x names", {"tool_name": "Bash", "tool_input": {"command": "declare -x | cut -d= -f1"}}, True),
    (
        "env names filtered to identifiers",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env names filtered anchored",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "env | sort | cut -d= -f1 | grep -E '^[A-Za-z_][A-Za-z0-9_]*$' | head -3"},
        },
        True,
    ),
    (
        "env malformed names printed",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -vxE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env malformed names counted",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -cvxE '[A-Za-z_][A-Za-z0-9_]*'"}},
        False,
    ),
    (
        "env identifier lines without name stage",
        {"tool_name": "Bash", "tool_input": {"command": "env | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env names loose filter",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -E '[A-Za-z_]+'"}},
        True,
    ),
    ("compgen exported names", {"tool_name": "Bash", "tool_input": {"command": "compgen -e"}}, False),
    ("safe env names form", {"tool_name": "Bash", "tool_input": {"command": ENV_NAMES_SAFE}}, False),
    ("env head raw", {"tool_name": "Bash", "tool_input": {"command": "env | head -3"}}, True),
    ("printenv PATH", {"tool_name": "Bash", "tool_input": {"command": "printenv PATH"}}, False),
    ("printenv secret", {"tool_name": "Bash", "tool_input": {"command": "printenv DB_PASSWORD"}}, True),
    ("echo secret var", {"tool_name": "Bash", "tool_input": {"command": 'echo "$KB_ROUTER_TOKEN"'}}, True),
    ("echo plain var", {"tool_name": "Bash", "tool_input": {"command": 'echo "$HOME"'}}, False),
    (
        "curl with token",
        {"tool_name": "Bash", "tool_input": {"command": 'curl -H "Authorization: Bearer $API_TOKEN" http://x'}},
        False,
    ),
    ("docker exec cat env", {"tool_name": "Bash", "tool_input": {"command": "docker exec app cat /app/.env"}}, True),
    (
        "kubectl exec cat env",
        {"tool_name": "Bash", "tool_input": {"command": "kubectl exec pod -- cat /app/.env"}},
        True,
    ),
    ("ssh cat bashrc", {"tool_name": "Bash", "tool_input": {"command": "ssh host cat ~/.bashrc"}}, True),
    ("compose config", {"tool_name": "Bash", "tool_input": {"command": "docker compose config"}}, True),
    (
        "compose no-interpolate",
        {"tool_name": "Bash", "tool_input": {"command": "docker compose config --no-interpolate"}},
        False,
    ),
    ("docker inspect bare", {"tool_name": "Bash", "tool_input": {"command": "docker inspect litellm"}}, True),
    (
        "docker inspect format",
        {"tool_name": "Bash", "tool_input": {"command": "docker inspect --format '{{.State.Status}}' litellm"}},
        False,
    ),
    ("kubectl secret yaml", {"tool_name": "Bash", "tool_input": {"command": "kubectl get secret db -o yaml"}}, True),
    ("kubectl secret list", {"tool_name": "Bash", "tool_input": {"command": "kubectl get secrets -n prod"}}, False),
    ("kubectl describe secret", {"tool_name": "Bash", "tool_input": {"command": "kubectl describe secret db"}}, True),
    ("access env printed", {"tool_name": "Bash", "tool_input": {"command": "agentihooks access env"}}, True),
    (
        "access env module printed",
        {"tool_name": "Bash", "tool_input": {"command": "/v/bin/python -m scripts.access_env env | cat"}},
        True,
    ),
    (
        "access env captured",
        {"tool_name": "Bash", "tool_input": {"command": 'x="$(agentihooks access env)"; echo "$x"'}},
        True,
    ),
    (
        "access env evaluated",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(agentihooks access env)" && test -n "$A"'}},
        False,
    ),
    (
        "access env module evaluated",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(/v/bin/python -m scripts.access_env env)"'}},
        False,
    ),
    (
        "access env evaluated then printed",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(agentihooks access env)"; agentihooks access env'}},
        True,
    ),
    ("access help", {"tool_name": "Bash", "tool_input": {"command": "agentihooks access envelope"}}, False),
    (
        "access env called from python",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -c 'from scripts import access_env; access_env.main([\"env\"])'"},
        },
        True,
    ),
    (
        "access env values from python",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c 'print(access_env.values (env))'"}},
        True,
    ),
    (
        "access env source linted",
        {"tool_name": "Bash", "tool_input": {"command": "ruff check scripts/access_env.py"}},
        False,
    ),
    (
        "python calls env helper",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "auth.env_text(record)"'}},
        False,
    ),
    (
        "python references env attribute",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "print(auth.env_name)"'}},
        False,
    ),
    (
        "python references credential attribute",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "print(auth.netrc_path)"'}},
        False,
    ),
    (
        "python reads member dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('auth.env').read())\""}},
        True,
    ),
    (
        "python reads dotenv beside helper call",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"auth.env_text(open('/app/auth.env').read())\""}},
        True,
    ),
    (
        "python reads named dotenv variant",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -c \"print(open('/app/.env.production-local').read())\""},
        },
        True,
    ),
    (
        "python reads shell config",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('/home/user/.bashrc').read())\""}},
        True,
    ),
    (
        "python reads credential file",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('/home/user/.netrc').read())\""}},
        True,
    ),
    (
        "python opens dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('.env').read())\""}},
        True,
    ),
    ("chained cat dotenv", {"tool_name": "Bash", "tool_input": {"command": "cd /app && cat .env"}}, True),
    ("git status", {"tool_name": "Bash", "tool_input": {"command": "git status --short"}}, False),
    ("kubectl logs", {"tool_name": "Bash", "tool_input": {"command": "kubectl logs -f pod-0 -n ns"}}, False),
    ("empty command", {"tool_name": "Bash", "tool_input": {"command": ""}}, False),
    ("no tool_input", {"tool_name": "Bash"}, False),
    ("unknown tool", {"tool_name": "WebFetch", "tool_input": {"url": "http://x/.env"}}, False),
    ("malformed payload", {}, False),
    (
        "heredoc mentioning a renderer",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 - <<'PY'\nprint('" + "docker " + "compose config')\nPY"},
        },
        False,
    ),
    (
        "heredoc mentioning a dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 - <<'PY'\nprint('read the .env file')\nPY"}},
        False,
    ),
    # naming a credential file is not reading it
    (
        "find names dotenv",
        {"tool_name": "Bash", "tool_input": {"command": 'find . -name ".env" | grep -v example'}},
        False,
    ),
    (
        "grep regex in source",
        {"tool_name": "Bash", "tool_input": {"command": "grep -rn '\\.env' scripts/install.py"}},
        False,
    ),
    (
        "find split quotes",
        {"tool_name": "Bash", "tool_input": {"command": "find . -name '.en''v' -printf '%h\\n' | sort -u"}},
        False,
    ),
    ("rg absolute dir", {"tool_name": "Bash", "tool_input": {"command": "rg -n pat /abs/dir"}}, False),
    ("ls dot", {"tool_name": "Bash", "tool_input": {"command": "ls -la ."}}, False),
    (
        "grep secret name in source",
        {"tool_name": "Bash", "tool_input": {"command": "grep -rn 'API_KEY' src/main.py"}},
        False,
    ),
    ("find py xargs cat", {"tool_name": "Bash", "tool_input": {"command": "find . -name '*.py' | xargs cat"}}, False),
    ("echo dot", {"tool_name": "Bash", "tool_input": {"command": "echo . && ls"}}, False),
    ("grep exclude dotenv", {"tool_name": "Bash", "tool_input": {"command": "grep -rn --exclude=.env X ."}}, False),
    ("rg negative glob", {"tool_name": "Bash", "tool_input": {"command": "rg -g '!.env' X ."}}, False),
    ("git add dotenv", {"tool_name": "Bash", "tool_input": {"command": "git add .env"}}, False),
    ("docker env-file", {"tool_name": "Bash", "tool_input": {"command": "docker run --env-file .env img"}}, False),
    # reads that the operand-aware rules must still catch
    ("cat redirect dotenv", {"tool_name": "Bash", "tool_input": {"command": "cat < .env"}}, True),
    ("cat attached redirect", {"tool_name": "Bash", "tool_input": {"command": "cat <.env"}}, True),
    ("grep in dotenv file", {"tool_name": "Bash", "tool_input": {"command": "grep TOKEN .env"}}, True),
    ("git diff no-index", {"tool_name": "Bash", "tool_input": {"command": "git diff --no-index .env x"}}, True),
    ("git show rev path", {"tool_name": "Bash", "tool_input": {"command": "git show HEAD:.env"}}, True),
    ("git grep dotenv", {"tool_name": "Bash", "tool_input": {"command": "git grep X -- .env"}}, True),
    ("git -C diff dotenv", {"tool_name": "Bash", "tool_input": {"command": "git -C /repo diff .env"}}, True),
    (
        "git blame option value",
        {"tool_name": "Bash", "tool_input": {"command": "git blame --ignore-revs-file=.env f"}},
        True,
    ),
    ("bash -c cat", {"tool_name": "Bash", "tool_input": {"command": "bash -c 'cat .env'"}}, True),
    ("source zshrc", {"tool_name": "Bash", "tool_input": {"command": "source ~/.zshrc"}}, True),
    ("find dotenv xargs cat", {"tool_name": "Bash", "tool_input": {"command": "find . -name .env | xargs cat"}}, True),
    (
        "find dotenv exec cat",
        {"tool_name": "Bash", "tool_input": {"command": "find . -name '.env*' -exec cat {} \\;"}},
        True,
    ),
    ("process substitution", {"tool_name": "Bash", "tool_input": {"command": "awk 1 <(cat .env)"}}, True),
    ("dollar redirect", {"tool_name": "Bash", "tool_input": {"command": 'echo "$(<.env)"'}}, True),
    ("command substitution", {"tool_name": "Bash", "tool_input": {"command": 'echo "$(cat .env)"'}}, True),
    ("cat dotfile glob", {"tool_name": "Bash", "tool_input": {"command": "cat .*"}}, True),
    ("grep -r home", {"tool_name": "Bash", "tool_input": {"command": "grep -r X ~"}}, True),
    ("grep -f attached", {"tool_name": "Bash", "tool_input": {"command": "sed -n 1p -f.env x"}}, True),
    ("ssh sudo cat", {"tool_name": "Bash", "tool_input": {"command": "ssh host sudo cat /root/.netrc"}}, True),
    (
        "kubectl exec sh -c env grep sed",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": "H=$(kubectl get pods -n swarm-hive-001 -o name | grep worker-hive | head -1); "
                'kubectl exec -n swarm-hive-001 $H -- sh -c \'env | grep -iE "ledger|swarm_url|8765|8766" | '
                'sed -E "s/(TOKEN|KEY|SECRET|PASS)[^=]*=.*/\\1=<redacted>/"\' 2>&1 | '
                'grep -viE "token|key|secret|pass" | head -10'
            },
        },
        True,
    ),
    (
        "kubectl exec sh -c env cut grep",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": 'kubectl -n swarm-hive-001 exec "$P" -c worker-hive -- sh -c '
                "'env | cut -d= -f1 | grep -i -E \"ca|cert|ssl|ledger|swarm_url\" | sort' 2>&1 | head -30"
            },
        },
        True,
    ),
    (
        "awk ENVIRON value print",
        {
            "tool_name": "Bash",
            "tool_input": {
                "command": 'agentihooks hive list 2>&1 | head -30; echo "---"; hostname; '
                "awk 'BEGIN{for(k in ENVIRON) if(k ~ /^(AGENTIHOOKS_(HIVE|SWARM|AGENT_NAME|SWARM_TASK|COMPACT_LIMIT)"
                '[A-Z_]*)$/) print k"="ENVIRON[k]}\''
            },
        },
        True,
    ),
    (
        "kubectl exec sh -c ls",
        {"tool_name": "Bash", "tool_input": {"command": "kubectl exec pod -- sh -c 'ls /app'"}},
        False,
    ),
]


@pytest.mark.parametrize("label,payload,should_block", CASES, ids=[c[0] for c in CASES])
def test_guard_decision(label, payload, should_block):
    verdict = decide(payload)
    assert (verdict is not None) is should_block


def test_guard_never_raises_on_garbage():
    for payload in ({}, {"tool_name": None}, {"tool_input": "notadict"}, {"tool_name": "Bash", "tool_input": None}):
        decide(payload)
        evaluate(payload, allow_rewrite=True)


def test_access_env_block_names_the_token_and_the_eval_form():
    reason = decide({"tool_name": "Bash", "tool_input": {"command": "agentihooks access env"}})

    assert reason == (
        block("printing the Cloudflare Access service token (agentihooks access env)", KIND_ENVIRONMENT)
        + '\nEvaluate it instead:  eval "$(agentihooks access env)", then test each variable by name.'
    )
    assert reason.startswith("BLOCKED: printing the Cloudflare Access service token (agentihooks access env).\n")
    assert reason != block("printing the Cloudflare Access service token (agentihooks access env)", None) + (
        '\nEvaluate it instead:  eval "$(agentihooks access env)", then test each variable by name.'
    )


GET_SECRET_VALUE_RENDERERS = [
    "kubectl get secret db --output yaml",
    "kubectl get secret db --output=yaml",
    "kubectl get secret db -o=json",
    "kubectl get secret db -o 'yaml'",
    'kubectl get secret db -o "json"',
    "kubectl get secret db -o kyaml",
    "kubectl get secret db -oyaml",
    "kubectl get secrets -o json",
    "kubectl get secret db -o jsonpath={.data}",
    "kubectl get secret db -o jsonpath-as-json={.data}",
    "kubectl get secret db -o=jsonpath={.data.password}",
    "kubectl get secret db --output=jsonpath={.data.password}",
    "kubectl get secret db -o go-template={{.data.password}}",
    "kubectl get secret db -o template='{{.data.password}}'",
    "kubectl get secret db --template='{{.data.password}}'",
    "kubectl get secret db -o go-template --template '{{.data.password}}'",
    "kubectl get secret db -o go-template='{{range $k,$v := .data}}{{$v}} {{end}}'",
    "kubectl get secret db -o go-template='{{range $k := .data}}{{$k}} {{end}}'",
    "kubectl get secret db -o go-template='{{range $k,$v := .data}}{{$k}}={{$v}} {{end}}'",
    "kubectl get secret db -o go-template='{{range $k,$k := .data}}{{$k}} {{end}}'",
    "kubectl get secret db -o go-template='{{.metadata.annotations}}'",
    "kubectl get secret db -o go-template-file=values.tmpl",
    "kubectl get secret db -o templatefile=values.tmpl",
    "kubectl get secret db -o custom-columns=V:.data.password",
    "kubectl get secret db -o custom-columns-file=columns.txt",
    "kubectl get secret db -o $FORMAT",
    "kubectl get -n prod secret db -o yaml",
    "kubectl get --namespace=prod secrets -o json",
    "kubectl get configmap,secret -o yaml",
    "kubectl get -o yaml secret db",
    "kubectl get Secret/db --output=json",
    "kubectl get secrets.v1. db --output=yaml",
    "kubectl get secret db \\\n  --output yaml",
    "kubectl get secret db -o=json | jq .data",
    'echo "$(kubectl get secret db -o=json)"',
    "ssh hive-002 'kubectl get secret db --output=yaml'",
    "kubectl get pods && kubectl get secret db -o=yaml",
    "kubectl get sec\\\nret db -o yaml",
    "kubectl get secret db -o yaml -o name",
    "kubectl get secret db -o name -o yaml",
    "kubectl get secret db -o go-template",
    "kubectl get secret db -o go-template='{{range $k,$v := .metadata.annotations}}{{$k}}{{end}}'",
    "kubectl get secret db -o 'go-template={{range $k,$v := .data}}{{$k}} {{end}}' --template='{{.data.password}}'",
    "kubectl get secret db -o==yaml",
    "kubectl get secrets -Ao yaml",
    "kubectl -o yaml get secret db",
    "kubectl get $(echo secret) db -o yaml",
    "echo secret db | xargs kubectl get -o yaml",
    "R=secret; kubectl get $R db -o yaml",
    "kubectl get $'secret' db -o yaml",
    "kubectl get -f manifests/ -o json",
    "kubectl get --raw /api/v1/namespaces/prod/secrets/db",
    "kubectl get --raw=/api/v1/secrets",
    "kubectl get -w $R db -o yaml",
    "R=secret; kubectl get --show-labels $R db -o yaml",
    "kubectl get --ignore-not-found $'secret' db -o yaml",
    "kubectl get -Ao yaml $R",
    "kubectl get --client-key k $R db -o yaml",
    "kubectl get --username u $R db -o yaml",
    "kubectl get -An prod $R db -o yaml",
    "kubectl get -L app $R db -o yaml",
    "kubectl get --unknown-value v $R db -o yaml",
]

GET_SECRET_KEYS_AND_NAMES = [
    "kubectl get secret db",
    "kubectl get secrets -n prod",
    "kubectl get secrets -o name",
    "kubectl get secrets -o wide",
    "kubectl get secrets -oname",
    "kubectl get secrets -o \\\n  name",
    "kubectl get secrets --output",
    "kubectl get secret db -o go-template='{{range $k,$v := .data}}{{$k}} {{end}}'",
    "kubectl get secret db --template='{{range $k,$v:=.data}}{{$k}} {{end}}'",
    "kubectl get secret db -o template --template '{{range $key, $_ := .data}}{{$key}}{{\"\\n\"}}{{end}}'",
    "kubectl get secrets -o name | xargs -n1 echo",
    "echo $(kubectl get secrets -o name)",
    "kubectl get pods -o yaml",
    "kubectl get configmap app -o jsonpath={.data}",
    "kubectl get pod secret-store-0 -o yaml",
    "kubectl get pods -o name | grep -o secret",
    "kubectl get pods -o name; echo secret -o yaml",
    "kubectl -n secrets get pods -o yaml",
    "kubectl get pods -l app=$APP -o yaml",
    "kubectl get deploy/$NAME -o yaml",
    "kubectl get --raw /healthz",
    "kubectl get -w pods -o yaml",
    "kubectl get pods -n $NS --selector $SEL -o yaml",
    "kubectl get -n $NS pods -o yaml",
    "kubectl get -oyaml pods",
    "kubectl get --client-key k pods -o yaml",
    "kubectl get pod/$POD -o yaml",
    "kubectl get pods -n",
]


@pytest.mark.feature("credential-guard-refuses-get-secret-values")
@pytest.mark.parametrize("command", GET_SECRET_VALUE_RENDERERS)
def test_kubectl_get_secret_forms_that_print_a_value_are_refused(command: str) -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason is not None and reason.startswith(
        block("printing Secret data (kubectl get secret -o yaml/json)", KIND_ENVIRONMENT)
    )


@pytest.mark.feature("credential-guard-refuses-get-secret-values")
@pytest.mark.parametrize("command", GET_SECRET_KEYS_AND_NAMES)
def test_kubectl_get_secret_forms_that_print_no_value_pass(command: str) -> None:
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


GET_NON_SECRET_DIAGNOSTICS = [
    "kubectl get pod mypod -n ns -o jsonpath='{.status.phase}'",
    "kubectl get pods -n ns -o custom-columns=NAME:.metadata.name,STATUS:.status.phase",
    "kubectl get priorityclass -o jsonpath='{.items[*].metadata.name}'",
    "kubectl get pod \"$POD\" -n ns -o jsonpath='{.status.phase}'",
    "kubectl get pod $POD -o jsonpath='{.status.phase}'",
    "kubectl -n ns get pod \"$POD\" -o jsonpath='{.status.phase}'",
    "kubectl get pods --namespace=$NS \"$POD\" -o jsonpath='{.status.phase}'",
    "kubectl get priorityclass \"$PC\" -o jsonpath='{.value}'",
    'kubectl get pods "$POD" -n ns -o custom-columns=NAME:.metadata.name,STATUS:.status.phase',
    "kubectl get --unknown-value v pod $POD -o yaml",
    "kubectl get -n ns pod \"$POD\" -o jsonpath='{.status.phase}'",
    "kubectl get --chunk-size=50 pod \"$POD\" -o jsonpath='{.status.phase}'",
    "kubectl get pod secret -o jsonpath='{.status.phase}'",
    "kubectl get pods -n secret -o custom-columns=NAME:.metadata.name,STATUS:.status.phase",
    "kubectl get pods --namespace=secrets -o jsonpath='{.items[*].metadata.name}'",
    "kubectl get -A pods -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name",
    "kubectl get pods -n get -o jsonpath='{.items[*].metadata.name}'",
    "kubectl get pod get -o jsonpath='{.status.phase}'",
    "kubectl get secrets -o name | grep -o yaml",
    'kubectl get priorityclass -o jsonpath=\'{range .items[*]}{.metadata.name}{";"}{.value}{"\\n"}{end}\'',
    'kubectl get pods -A -o jsonpath=\'{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"\\n"}{end}\'',
    "kubectl get pods -o jsonpath=\"{.items[?(@.status.phase=='Running')].metadata.name}{'&'}\"",
    'ssh hive-002 \'kubectl get pod "$POD" -o jsonpath="{.status.phase}"\'',
]

GET_SECRET_TYPE_FORMS = [
    "kubectl get secret \"$NAME\" -o jsonpath='{.data}'",
    "kubectl get secrets -o jsonpath='{range .items[*]}{.data}{\";\"}{end}'",
    "kubectl get pods,secrets -A -o jsonpath='{.items[*].data}'",
    "kubectl get -n ns $R db -o jsonpath='{.data}'",
    "kubectl get deploy/app $X -o yaml",
    "kubectl get --unknown-flag secret db -o yaml",
    "kubectl -n get get secret db -o jsonpath='{.data}'",
    "kubectl -n get get $R db -o yaml",
    "kubectl exec mypod -- kubectl get secret db -o yaml",
    'kubectl exec mypod -- sh -c "kubectl get pods; kubectl get secret db -o yaml"',
    "kubectl exec mypod -- sh -c 'kubectl get secret db -o yaml'",
    "kubectl get --show-labels $R -o jsonpath='{.data}'",
    "kubectl get secret db \\' -o yaml",
    "kubectl get --unknown-value v $R db -o jsonpath='{.data}{\";\"}'",
]


@pytest.mark.feature("credential-guard-get-reads-the-type-operand")
@pytest.mark.parametrize("command", GET_NON_SECRET_DIAGNOSTICS)
def test_kubectl_get_of_a_named_non_secret_type_passes_with_any_output(command: str) -> None:
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


@pytest.mark.feature("credential-guard-get-reads-the-type-operand")
@pytest.mark.parametrize("command", GET_SECRET_TYPE_FORMS)
def test_kubectl_get_whose_type_operand_may_be_a_secret_is_refused(command: str) -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason is not None and reason.startswith(
        block("printing Secret data (kubectl get secret -o yaml/json)", KIND_ENVIRONMENT)
    )


GH_TOKEN_RENDERERS = [
    "gh auth status",
    "gh auth status --show-token",
    "gh auth status -t",
    "gh auth status -h github.com",
    "gh --hostname github.com auth status",
    "gh auth token",
    "/usr/bin/gh auth status",
    "sudo gh auth status",
    "GH_TOKEN=x gh auth status",
    "env GH_HOST=github.com gh auth token",
    "gh auth status 2>&1 | head -5",
    "cd /repo && gh auth status",
    'echo "$(gh auth token)"',
    "bash -c 'gh auth status'",
    "ssh hive-002 gh auth status",
    "docker exec worker gh auth status",
    "kubectl exec pod-0 -- gh auth status",
    "ssh hive-002 'gh auth status'",
    "ssh hive-002 gh --hostname github.com auth status",
    "gh -R owner/repo auth token",
]

GH_LEAK_FREE = [
    "gh auth login --with-token < /dev/null",
    "gh auth setup-git",
    "gh pr view 7843",
    "gh pr list --search auth token",
    "gh help auth status",
    "gh -R owner/repo help auth status",
    "gh",
    'gh issue comment 1 --body "auth status"',
    "gh api rate_limit --jq .rate.remaining",
    'test -n "${GH_TOKEN:-}" && echo set || echo unset',
    "grep -rn 'gh auth status' docs",
    "git log --grep 'gh auth token'",
]


@pytest.mark.parametrize("command", GH_TOKEN_RENDERERS)
def test_gh_commands_that_print_a_token_are_refused(command):
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason == (
        block("printing the GitHub token (gh auth status or gh auth token)", KIND_ENVIRONMENT)
        + '\nTo test whether the token is set:  test -n "${GH_TOKEN:-}" && echo set || echo unset'
        + "\nTo test whether it works:  gh api rate_limit --jq .rate.remaining"
    )


@pytest.mark.parametrize("command", GH_LEAK_FREE)
def test_gh_commands_that_print_no_token_pass(command):
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


SECRET_KEY_LISTING = (
    "\nkubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}' "
    "lists the key names without the values."
)

DESCRIBE_SECRET_COMMANDS = [
    "kubectl describe secret db",
    "kubectl describe secrets",
    "kubectl describe Secret/db",
    "kubectl -n prod describe secret db",
    "kubectl describe -n prod secret db",
    "kubectl describe --namespace=prod secrets db",
    "kubectl describe --context prod secret db",
    "kubectl describe configmaps,secrets",
    "kubectl describe secrets.v1. db",
    "kubectl describe pod/web secret/db",
    "kubectl describe \\\n  secret db",
    "kubectl \\\n  describe secret db",
    'kubectl describe "sec"ret db',
    "kubectl describe secret db 2>&1",
    'ssh host "kubectl describe secret db"',
    "sudo kubectl describe secret db | head",
    "kubectl describe pod web | kubectl describe secret db",
    'echo "$(kubectl describe secrets)"',
    "kubectl get secrets -o name | xargs kubectl describe",
    'for s in $(kubectl get secrets -o name); do kubectl describe "$s"; done',
    "kubectl describe $RES db",
    "kubectl describe pod/web $NAME",
    "kubectl describe -f deploy.yaml",
    "kubectl describe --filename=deploy.yaml",
    "kubectl describe -k overlays/prod",
    "kubectl describe --kustomize dir",
    "kubectl describe\\\n  secret db",
    "kubectl describe sec\\ret db",
    "kubectl describe --kubeconfig x $RES",
    "kubectl describe -s x $RES",
]

DESCRIBE_ALLOWED_COMMANDS = [
    "kubectl describe pod secret-store-0",
    "kubectl describe externalsecret db",
    "kubectl describe sa default",
    "kubectl describe pod web | grep -i secret",
    "kubectl describe pod web > secret",
    "kubectl -n secret describe pod web",
    "kubectl describe pod $POD",
    "kubectl describe pod/$NAME",
    "kubectl describe -n $NS pod web",
    "kubectl describe --namespace $NS pod web",
    "kubectl describe -l $SEL pods",
    "kubectl describe --selector $SEL pods",
    "kubectl describe --selector=$SEL pods",
    "kubectl describe --context $CTX pod web",
    "kubectl describe -A pods",
    "kubectl describe --all-namespaces pods",
    "kubectl get pods | grep describe",
    "kubectl logs web-describe-0",
    "kubectl get secret db --template='{{range $k,$v:=.data}}{{$k}} {{end}}'",
]


@pytest.mark.feature("credential-guard-refuses-describe-secret")
@pytest.mark.parametrize("command", DESCRIBE_SECRET_COMMANDS)
def test_kubectl_describe_of_a_secret_is_refused(command: str) -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason == (
        block(
            "describing a Secret or a resource the command does not name "
            "(kubectl describe prints the token of a service account token Secret)",
            KIND_ENVIRONMENT,
        )
        + SECRET_KEY_LISTING
    )


@pytest.mark.feature("credential-guard-refuses-describe-secret")
@pytest.mark.parametrize("command", DESCRIBE_ALLOWED_COMMANDS)
def test_kubectl_commands_that_name_no_secret_type_pass(command: str) -> None:
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


@pytest.mark.feature("credential-guard-refuses-describe-secret")
def test_get_secret_refusal_offers_the_key_listing_and_not_describe() -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": "kubectl get secret db -o yaml"}})

    assert reason == (
        block("printing Secret data (kubectl get secret -o yaml/json)", KIND_ENVIRONMENT) + SECRET_KEY_LISTING
    )


DESCRIBE_FLAG_WORD_REFUSED_COMMANDS = [
    "kubectl describe --show-events $RES db",
    "kubectl describe --recursive $RES db",
    "kubectl describe -R $RES db",
    "kubectl describe -AR $RES db",
    "kubectl describe --insecure-skip-tls-verify $RES db",
    "kubectl describe -nprod $RES db",
    "kubectl describe -An $NS $RES db",
    "kubectl describe -Rn prod $RES db",
    "kubectl describe --as admin $RES db",
    "kubectl describe --chunk-size 500 $RES db",
    "kubectl describe -v 6 $RES db",
]

DESCRIBE_FLAG_WORD_ALLOWED_COMMANDS = [
    "kubectl describe --show-events pod $POD",
    "kubectl describe -R pod $POD",
    "kubectl describe --show-events=false pod web",
    "kubectl describe -An $NS pods",
    "kubectl describe -nprod pod web",
]


@pytest.mark.feature("credential-guard-describe-flag-words")
@pytest.mark.parametrize("command", DESCRIBE_FLAG_WORD_REFUSED_COMMANDS)
def test_a_hidden_describe_type_after_any_flag_is_refused(command: str) -> None:
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason == (
        block(
            "describing a Secret or a resource the command does not name "
            "(kubectl describe prints the token of a service account token Secret)",
            KIND_ENVIRONMENT,
        )
        + SECRET_KEY_LISTING
    )


@pytest.mark.feature("credential-guard-describe-flag-words")
@pytest.mark.parametrize("command", DESCRIBE_FLAG_WORD_ALLOWED_COMMANDS)
def test_a_named_describe_type_after_any_flag_passes(command: str) -> None:
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


def _bash(command, cwd=""):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


SHELL_SCRIPT_DUMPS = [
    ("sh -c 'env | sort'", "dumping the whole environment (env)"),
    ("bash -lc 'printenv | grep REDIS'", "dumping the whole environment (printenv)"),
    ("zsh -c 'export -p'", "dumping the whole environment (export)"),
    ("dash -ec 'set'", "dumping the whole environment (set)"),
    ("bash -ce 'env'", "dumping the whole environment (env)"),
    ("sudo bash -euo pipefail -c 'env | cut -d= -f1'", "dumping the whole environment (env)"),
    ("docker exec box bash -c 'printenv'", "dumping the whole environment (printenv)"),
    ("ssh host sh -c 'env'", "dumping the whole environment (env)"),
    (
        "kubectl exec -n ns pod -- sh -c 'env | cut -d= -f1 | grep -i -E \"ca|cert\" | sort' 2>&1 | head -30",
        "dumping the whole environment (env)",
    ),
    ("kubectl exec pod -- sh -c \"awk '{print ENVIRON[$1]}'\"", "printing environment values (awk ENVIRON[...])"),
]

SHELL_SCRIPTS_THAT_PRINT_NO_VALUE = [
    "kubectl exec -n ns pod -- sh -c 'ls /app'",
    "bash -c 'env | wc -l'",
    "bash -c 'compgen -e'",
    "bash script.sh",
    "bash script.sh check env",
    "bash --rcfile env",
    "bash -c",
    "grep -c env notes.txt",
    "python3 -c 'env'",
]


@pytest.mark.feature("credential-guard-reads-shell-scripts")
@pytest.mark.parametrize(("command", "what"), SHELL_SCRIPT_DUMPS)
def test_an_environment_dump_inside_a_shell_script_is_refused(command: str, what: str) -> None:
    assert decide(_bash(command)) == block(what, KIND_ENVIRONMENT)


@pytest.mark.feature("credential-guard-reads-shell-scripts")
@pytest.mark.parametrize("command", SHELL_SCRIPTS_THAT_PRINT_NO_VALUE)
def test_a_shell_script_that_prints_no_value_passes(command: str) -> None:
    assert decide(_bash(command)) is None


@pytest.mark.feature("credential-guard-reads-shell-scripts")
def test_a_shell_script_keeps_the_credential_tree_scan(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")

    verdict = evaluate(_bash(f"bash -c 'grep -r needle {tmp_path}'"))

    assert verdict.block == block(f"searching a tree that holds {tmp_path / '.env'}", KIND_RECURSIVE)


HEREDOC_DELIMITER_LINE_READS = [
    ("cat <<EOF > out; cat ~/.env\nbody\nEOF", "cat ~/.env"),
    ("cat <<'EOF' && cat .env\nbody\nEOF", "cat .env"),
    ("cat <<-EOF > out || cat .env\n\tbody\n\tEOF", "cat .env"),
    ('tee notes <<"EOF" | cat - .env\nbody\nEOF', "cat - .env"),
]

HEREDOCS_A_SHELL_RUNS = [
    ("cat <<EOF | bash\ncat ~/.env\nEOF", "cat ~/.env"),
    ("cat <<'EOF' | sh\ncat .env\nEOF", "cat .env"),
    ("bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("sh -s <<'X'\nenv\nX", "env"),
    ("sudo bash <<EOF\nsource ~/.bashrc\nEOF", "source ~/.bashrc"),
    ("kubectl exec -i pod -- sh <<EOF\nprintenv\nEOF", "printenv"),
    ("ssh host 'bash -s' <<'EOF'\ncat .env\nEOF", "cat .env"),
    ("/bin/zsh <<EOF\necho ok\ncat .env\nEOF", "cat .env"),
    ('out="$(dash <<EOF\ncat .env\nEOF\n)"', "cat .env"),
    ('bash -c "sh <<EOF\ncat .env\nEOF\n"', "cat .env"),
    ("source /dev/stdin <<EOF\ncat ~/.env\nEOF", "cat ~/.env"),
    (". /dev/stdin <<'EOF'\nenv\nEOF", "env"),
    ('eval "$(cat <<EOF\ncat ~/.env\nEOF\n)"', "cat ~/.env"),
    ("ksh <<EOF\ncat .env\nEOF", "cat .env"),
    ("mksh <<EOF\ncat .env\nEOF", "cat .env"),
    ("ash <<EOF\ncat .env\nEOF", "cat .env"),
    ("fish <<EOF\ncat .env\nEOF", "cat .env"),
    ("csh <<EOF\ncat .env\nEOF", "cat .env"),
    ("tcsh <<EOF\ncat .env\nEOF", "cat .env"),
    ("su <<EOF\ncat .env\nEOF", "cat .env"),
    ("cat <<EOF | sudo -u root bash\ncat .env\nEOF", "cat .env"),
    ("doas -u root sh <<EOF\ncat .env\nEOF", "cat .env"),
    ("cat <<EOF | /usr/bin/env bash\ncat .env\nEOF", "cat .env"),
    ("env -i bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("command -p bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("exec bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("time bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("nohup bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("nice -n 5 bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("timeout 5 bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("xargs bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("stdbuf -o0 bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("setsid bash <<EOF\ncat .env\nEOF", "cat .env"),
    ("busybox sh <<EOF\ncat .env\nEOF", "cat .env"),
]

HEREDOCS_THAT_READ_NO_CREDENTIAL = [
    "cat <<'EOF' > notes.md\ncat .env\nEOF",
    "cat <<EOF | grep x\ncat .env\nEOF",
    "python3 - <<'PY'\nprint('read the .env file')\nPY",
    "bash <<EOF\necho hi\nEOF",
    "cat <<EOF > out && echo done\nbody\nEOF",
    "git commit -F - <<'EOF'\nStop reading .env in the setup script\nEOF",
    "grep bash <<'EOF'\ncat .env\nEOF",
    "cat <<EOF | grep sh\ncat .env\nEOF",
    'bash -c "bash <<EOF\necho hi\nEOF\n"',
    "timeout 5 tee notes.md <<'EOF'\ncat .env\nEOF",
    "sudo -u root tee notes.md <<'EOF'\ncat .env\nEOF",
]


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
@pytest.mark.parametrize(("command", "read"), HEREDOC_DELIMITER_LINE_READS)
def test_a_credential_read_after_a_heredoc_delimiter_is_refused(command: str, read: str) -> None:
    reason = decide(_bash(command))

    assert reason is not None
    assert reason == decide(_bash(read))


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
@pytest.mark.parametrize(("command", "read"), HEREDOCS_A_SHELL_RUNS)
def test_a_credential_read_in_a_heredoc_a_shell_runs_is_refused(command: str, read: str) -> None:
    reason = decide(_bash(command))

    assert reason is not None
    assert reason == decide(_bash(read))


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
@pytest.mark.parametrize("command", HEREDOCS_THAT_READ_NO_CREDENTIAL)
def test_a_heredoc_that_reads_no_credential_passes(command: str) -> None:
    assert decide(_bash(command)) is None


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
def test_a_heredoc_a_shell_runs_keeps_the_credential_tree_scan(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")

    verdict = evaluate(_bash("bash <<'EOF'\ngrep -r needle .\nEOF", cwd=str(tmp_path)))

    assert verdict.block == block(f"searching a tree that holds {tmp_path}/./.env", KIND_RECURSIVE)


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
def test_a_rewrite_after_a_heredoc_delimiter_keeps_the_heredoc() -> None:
    command = "cat <<'EOF' > out && grep -rn needle .\nbody\nEOF\necho done"

    verdict = evaluate(_bash(command, cwd="/srv"), allow_rewrite=True)

    assert verdict.rewrite == {
        "command": "cat <<'EOF' > out && grep " + GREP_EXCLUDES + " -rn needle .\nbody\nEOF\necho done"
    }


@pytest.mark.feature("credential-guard-reads-heredoc-commands")
def test_stripping_heredocs_keeps_the_rest_of_the_delimiter_line() -> None:
    command = "cat <<'EOF' > out; git push\nbody\nEOF\nls"

    assert strip_heredocs(command) == "cat <<HEREDOC > out; git push\nls"


@pytest.mark.feature("credential-guard-heredoc-substitutions")
@pytest.mark.parametrize("delimiter", ["EOF", "-EOF"])
@pytest.mark.parametrize(
    "body",
    [
        "$(cat .env)",
        "`cat .env`",
        "$(<.env)",
        "'$(cat .env)'",
        '"$(cat .env)"',
        "before $(cat .env) after",
        "$(printf safe) $(cat .env)",
        "$(printf safe | cat .env)",
        "\\\\$(cat .env)",
    ],
    ids=[
        "dollar",
        "backtick",
        "redirect",
        "single-body-quotes",
        "double-body-quotes",
        "prose",
        "second",
        "pipeline",
        "escaped-backslash",
    ],
)
def test_an_unquoted_heredoc_credential_substitution_is_refused(delimiter: str, body: str) -> None:
    reason = decide(_bash(f"cat <<{delimiter}\n{body}\nEOF"))

    assert reason is not None
    assert reason == decide(_bash("cat .env"))


@pytest.mark.feature("credential-guard-heredoc-substitutions")
@pytest.mark.parametrize("delimiter", ["'EOF'", '"EOF"', "-'EOF'", '-"EOF"'])
@pytest.mark.parametrize("body", ["$(cat .env)", "`cat .env`", "$(<.env)"])
def test_a_quoted_heredoc_credential_substitution_stays_literal(delimiter: str, body: str) -> None:
    assert decide(_bash(f"cat <<{delimiter}\n{body}\nEOF")) is None


@pytest.mark.feature("credential-guard-heredoc-substitutions")
@pytest.mark.parametrize(
    "body",
    ["cat .env", "$(printf safe)", "`printf safe`", "<(cat .env)", "\\$(cat .env)", "\\`cat .env\\`", "plain text"],
    ids=[
        "literal-command",
        "safe-dollar",
        "safe-backtick",
        "literal-process",
        "escaped-dollar",
        "escaped-backticks",
        "plain",
    ],
)
def test_an_unquoted_heredoc_without_a_credential_substitution_passes(body: str) -> None:
    assert decide(_bash(f"cat <<EOF\n{body}\nEOF")) is None


@pytest.mark.feature("credential-guard-heredoc-substitutions")
def test_a_heredoc_substitution_keeps_the_credential_tree_scan(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("A=1\n")

    verdict = evaluate(_bash("cat <<EOF\n$(grep -r needle .)\nEOF", cwd=str(tmp_path)), allow_rewrite=True)

    assert verdict.block == block(f"searching a tree that holds {tmp_path}/./.env", KIND_RECURSIVE)
    assert verdict.rewrite is None


@pytest.mark.feature("credential-guard-heredoc-substitutions")
def test_a_spelled_heredoc_token_hides_no_later_heredoc_body() -> None:
    reason = decide(_bash('cat <<AHHEREDOC9 "\n" <<EOF\n$(cat .env)\nEOF'))

    assert reason is not None
    assert reason == decide(_bash("cat .env"))


AWK_ENVIRON_VALUE_READS = [
    "awk 'BEGIN{for(k in ENVIRON) if(k ~ /^AGENTIHOOKS_(HIVE|SWARM)/) print k\"=\"ENVIRON[k]}'",
    "gawk 'BEGIN{print ENVIRON[\"DATABASE_URL\"]}'",
    "mawk 'BEGIN { print ENVIRON [\"REDIS_URL\"] }'",
    "nawk 'BEGIN{print ENVIRON[\"X\"]}'",
    "compgen -e | /usr/bin/awk '{print $1\"=\"ENVIRON[$1]}'",
    "kubectl exec pod -- awk 'BEGIN{print ENVIRON[\"DATABASE_URL\"]}'",
]

AWK_ENVIRON_NAME_ONLY = [
    ENV_NAMES_SAFE,
    "awk 'BEGIN{for(k in ENVIRON) if(k ~ /LEDGER/) print k}'",
    "awk -F= '{print $1}' names.txt",
    "awk '{print MYENVIRON[1]}' data.txt",
    "grep -n 'ENVIRON[' notes.txt",
]


@pytest.mark.feature("credential-guard-refuses-awk-environ-values")
@pytest.mark.parametrize("command", AWK_ENVIRON_VALUE_READS)
def test_an_awk_read_of_an_environment_value_is_refused(command: str) -> None:
    assert decide(_bash(command)) == block("printing environment values (awk ENVIRON[...])", KIND_ENVIRONMENT)


@pytest.mark.feature("credential-guard-refuses-awk-environ-values")
@pytest.mark.parametrize("command", AWK_ENVIRON_NAME_ONLY)
def test_an_awk_listing_of_environment_names_passes(command: str) -> None:
    assert decide(_bash(command)) is None


POD_ENVIRONMENT_DUMPS = [
    ("kubectl exec mypod -- printenv GITHUB_TOKEN", "printing the value of GITHUB_TOKEN"),
    ("kubectl exec -n ns mypod -c app -- printenv API_KEY", "printing the value of API_KEY"),
    ("kubectl exec mypod -- env", "dumping the whole environment (env)"),
    ("kubectl exec -it mypod -- printenv", "dumping the whole environment (printenv)"),
    ("sudo kubectl -n ns exec mypod -- env | sort", "dumping the whole environment (env)"),
]

POD_ENVIRONMENT_NAME_ONLY = [
    "kubectl exec mypod -- " + ENV_NAMES_SAFE,
    "kubectl exec mypod -- env | wc -l",
    "kubectl exec mypod -- printenv HOME",
    "kubectl exec mypod -- ls env",
    "kubectl -n env exec mypod -- ls",
    "kubectl create job probe --image=busybox -- env",
]


@pytest.mark.feature("credential-guard-refuses-pod-environment-dumps")
@pytest.mark.parametrize(("command", "what"), POD_ENVIRONMENT_DUMPS)
def test_a_pod_environment_dump_without_a_shell_is_refused(command: str, what: str) -> None:
    assert decide(_bash(command)) == block(what, KIND_ENVIRONMENT)


@pytest.mark.feature("credential-guard-refuses-pod-environment-dumps")
@pytest.mark.parametrize("command", POD_ENVIRONMENT_NAME_ONLY)
def test_a_pod_environment_name_listing_passes(command: str) -> None:
    assert decide(_bash(command)) is None


SET_TESTS_IN_SCRIPTS = [
    "kubectl exec -n ns pod-0 -- sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
    "kubectl exec pod-0 -- bash -c '[ -n \"$GITHUB_TOKEN\" ] && echo set || echo unset'",
    "kubectl exec pod-0 -c worker -- sh -c 'test -z \"${API_KEY}\" && echo unset || echo set'",
    "kubectl exec pod-0 -- bash -c '[[ -n \"${LEDGER_TOKEN-}\" ]] && echo set || echo unset'",
    "kubectl exec pod-0 -- sh -c 'cd /app; test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
    "cat /dev/null | kubectl exec -i pod-0 -- sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
    "kubectl exec pod-0 -- sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'"
    " | kubectl exec -i pod-1 -- sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
    "docker exec box sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
    "sh -c 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'",
]

SCRIPTS_THAT_PRINT_THE_VALUE = [
    ("kubectl exec pod-0 -- sh -c 'echo $GITHUB_TOKEN'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'echo $github_token'", "github_token"),
    ("kubectl exec pod-0 -- sh -c 'echo test -n \"$GITHUB_TOKEN\"'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'printf %s test -n \"$GITHUB_TOKEN\"'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'echo [ -n \"$GITHUB_TOKEN\" ]'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'cd /app; echo x test -n \"$GITHUB_TOKEN\"'", "GITHUB_TOKEN"),
    ("V=\"$GITHUB_TOKEN\" sh -c 'echo \"$V\"' | sh -c 'wc -c'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'echo 'x' test -n \"$GITHUB_TOKEN\"'", "GITHUB_TOKEN"),
    ('echo test -n "$GITHUB_TOKEN"', "GITHUB_TOKEN"),
    ('echo "x;test -n "$GITHUB_TOKEN"\'"', "GITHUB_TOKEN"),
    ('kubectl exec pod-0 -- sh -c \'test -n "${GITHUB_TOKEN:-}" && echo "$GITHUB_TOKEN"\'', "GITHUB_TOKEN"),
    ('kubectl exec pod-0 -- sh -c \'test -n "$API_KEY" && echo set; printf %s "$API_KEY"\'', "API_KEY"),
    ('kubectl exec pod-0 -- sh -c \'test -n "${GITHUB_TOKEN:-}" && echo "$1"\' _ "$GITHUB_TOKEN"', "GITHUB_TOKEN"),
    ('kubectl exec pod-0 -- sh -c \'test -n "$GITHUB_TOKEN" -a "$GITHUB_TOKEN" -eq 0 && echo set\'', "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -c 'test -n \"$GITHUB_TOKEN\" -eq 0 && echo set'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- sh -xc 'test -n \"${GITHUB_TOKEN:-}\" && echo set || echo unset'", "GITHUB_TOKEN"),
    ("kubectl exec pod-0 -- bash -c 'set -o xtrace; [ -n \"$API_KEY\" ] && echo set'", "API_KEY"),
    ('test -n "${GITHUB_TOKEN:-}" && echo "$GITHUB_TOKEN"', "GITHUB_TOKEN"),
]

SET_TESTS_OF_ANOTHER_FORM = [
    "kubectl exec pod-0 -- sh -c 'test -n $GITHUB_TOKEN && echo set'",
    "kubectl exec pod-0 -- sh -c 'test -n \"${GITHUB_TOKEN:+x}\" && echo set'",
    r'kubectl exec pod-0 -- sh -c "test -n \"\$GITHUB_TOKEN\" && echo set"',
]


@pytest.mark.feature("credential-guard-passes-set-tests-in-scripts")
@pytest.mark.parametrize("command", SET_TESTS_IN_SCRIPTS)
def test_a_set_test_inside_a_shell_script_passes(command: str) -> None:
    assert decide(_bash(command)) is None


@pytest.mark.feature("credential-guard-passes-set-tests-in-scripts")
@pytest.mark.parametrize(("command", "name"), SCRIPTS_THAT_PRINT_THE_VALUE)
def test_a_shell_script_that_prints_the_value_is_refused(command: str, name: str) -> None:
    assert decide(_bash(command)) == block(f"printing ${name} to stdout", KIND_ENVIRONMENT)


@pytest.mark.feature("credential-guard-passes-set-tests-in-scripts")
@pytest.mark.parametrize("command", SET_TESTS_OF_ANOTHER_FORM)
def test_a_set_test_of_another_form_is_refused(command: str) -> None:
    assert decide(_bash(command)) == block("printing $GITHUB_TOKEN to stdout", KIND_ENVIRONMENT)


class TestEnvironmentNames:
    """A malformed variable name can hold a secret, so a name listing must drop it."""

    FAKE_NAME = "fake-leaked-name-0000"
    FAKE_VALUE = "fake-value-0000"

    def _run_safe_form(self):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "VALID_NAME": "x", self.FAKE_NAME: self.FAKE_VALUE}
        return subprocess.run(["bash", "-c", ENV_NAMES_SAFE], env=env, capture_output=True, text=True)

    def test_block_message_gives_the_safe_form(self):
        reason = decide(_bash("env | cut -d= -f1"))
        assert ENV_NAMES_SAFE in reason
        assert "env | cut -d= -f1  " not in reason

    def test_safe_form_lists_valid_names_and_counts_malformed(self):
        r = self._run_safe_form()
        lines = r.stdout.splitlines()
        assert r.returncode == 0
        assert "VALID_NAME" in lines
        assert "malformed names: 1" in lines

    def test_safe_form_never_prints_the_malformed_name_or_value(self):
        r = self._run_safe_form()
        assert self.FAKE_NAME not in r.stdout + r.stderr
        assert self.FAKE_VALUE not in r.stdout + r.stderr


class TestMultilineEnvValues:
    """A value spanning two lines turns its second line into a fake name in line based listings."""

    FAKE_FIRST = "fake-first-line-0000"
    FAKE_REST = "fake-rest-line-0000"
    LISTING = "env | cut -d= -f1"

    def _run(self, command):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MULTI_0001": self.FAKE_FIRST + "\n" + self.FAKE_REST}
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

    def test_unguarded_listing_would_print_the_second_line(self):
        assert self.FAKE_REST in self._run(self.LISTING).stdout.splitlines()

    def test_guard_blocks_the_listing(self):
        assert decide(_bash(self.LISTING))

    def test_block_message_names_multiline_values(self):
        assert "several lines" in decide(_bash(self.LISTING))

    def test_safe_form_never_prints_either_line(self):
        r = self._run(ENV_NAMES_SAFE)
        assert "FAKE_MULTI_0001" in r.stdout.splitlines()
        assert self.FAKE_FIRST not in r.stdout + r.stderr
        assert self.FAKE_REST not in r.stdout + r.stderr


class TestIdentifierShapedSecondLine:
    """A value's later line can look like a name, so no identifier filter makes a listing safe."""

    FAKE_FIRST = "fake-first-line-0000"
    FAKE_REST = "FAKE_REST_LINE_0000"
    FILTERED = "env | cut -d= -f1 | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"

    def _run(self, command):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MULTI_0002": self.FAKE_FIRST + "\n" + self.FAKE_REST}
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

    def test_unguarded_filtered_listing_would_print_the_second_line(self):
        assert self.FAKE_REST in self._run(self.FILTERED).stdout.splitlines()

    def test_guard_blocks_the_filtered_listing(self):
        assert decide(_bash(self.FILTERED))

    def test_safe_form_never_prints_the_second_line(self):
        r = self._run(ENV_NAMES_SAFE)
        assert "FAKE_MULTI_0002" in r.stdout.splitlines()
        assert self.FAKE_REST not in r.stdout + r.stderr

    def test_block_message_offers_only_the_awk_form_and_counts(self):
        reason = decide(_bash(self.FILTERED))
        assert ENV_NAMES_SAFE in reason
        assert "grep -c" in reason
        assert "grep -x" not in reason


class TestGrepLongOptionIsNotACount:
    """A long grep option holding c, l or q once passed as a count and printed values."""

    FAKE_VALUE = "fake-value-long-option-0000"
    COMMAND = "env | grep --color=never ."

    def test_unguarded_command_would_print_the_value(self):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_TOKEN_0003": self.FAKE_VALUE}
        r = subprocess.run(["bash", "-c", self.COMMAND], env=env, capture_output=True, text=True)
        assert "FAKE_TOKEN_0003=" + self.FAKE_VALUE in r.stdout.splitlines()

    def test_guard_blocks_it(self):
        assert decide(_bash(self.COMMAND))


class TestRecursiveRewrite:
    """Under bypass on claude the search keeps running; credential files leave it."""

    def test_grep_r_gains_excludes(self):
        v = evaluate(_bash("grep -rn X ."), allow_rewrite=True)
        assert v.block is None
        assert v.rewrite == {"command": "grep " + GREP_EXCLUDES + " -rn X ."}

    def test_rg_gains_negative_globs(self):
        v = evaluate(_bash("rg foo ."), allow_rewrite=True)
        assert v.rewrite == {"command": "rg " + RG_EXCLUDES + " foo ."}

    def test_compound_keeps_its_cd(self):
        v = evaluate(_bash("cd src && grep -r X . | head -5"), allow_rewrite=True)
        assert v.rewrite == {"command": "cd src && grep " + GREP_EXCLUDES + " -r X . | head -5"}

    def test_env_prefix_and_path_survive(self):
        v = evaluate(_bash("LC_ALL=C /usr/bin/grep -R X src"), allow_rewrite=True)
        assert v.rewrite == {"command": "LC_ALL=C /usr/bin/grep " + GREP_EXCLUDES + " -R X src"}

    def test_excludes_never_name_a_denied_home_path(self):
        # An option value is a file operand to the harness: --exclude=.bashrc
        # matches Read(~/.bashrc) and prompts. Keep those basenames out.
        from hooks.context.credential_guard import SHELL_RC

        for rc in SHELL_RC:
            assert rc not in GREP_EXCLUDES, rc
            assert rc not in RG_EXCLUDES, rc

    def test_the_grant_file_the_worker_home_writes_is_a_credential(self, tmp_path):
        from hooks.context.credential_guard import KIND_CREDENTIAL, sensitive_kind
        from scripts.swarm_v2.broadcast_bridge import grant_path

        assert sensitive_kind(str(grant_path(tmp_path / "exe-1"))) == KIND_CREDENTIAL

    def test_a_tool_input_naming_the_grant_file_is_near_a_credential(self):
        from hooks.context.credential_guard import near_credential
        from scripts.swarm_v2.broadcast_bridge import GRANT_NAME

        assert near_credential({"command": f"python3 -c \"open('run/{GRANT_NAME}')\""})
        assert not near_credential({"command": "python3 -c \"open('run/launch')\""})

    def test_recursive_search_leaves_the_worker_grant_out(self):
        from scripts.swarm_v2.broadcast_bridge import GRANT_NAME

        assert "--exclude=" + GRANT_NAME in GREP_EXCLUDES.split()
        assert "'!" + GRANT_NAME + "'" in RG_EXCLUDES.split()

    def test_quoted_delimiters_are_data(self):
        cmd = 'echo "a && grep -r x ." && git commit -m "b | rg y ."'
        assert evaluate(_bash(cmd), allow_rewrite=True).rewrite is None
        assert decide(_bash('echo "x; cat .env"')) is None

    def test_non_recursive_grep_untouched(self):
        assert evaluate(_bash("grep -n X file.py"), allow_rewrite=True) == (None, None, None)

    def test_filename_only_grep_untouched(self):
        assert evaluate(_bash("grep -rl X ."), allow_rewrite=True).rewrite is None

    def test_file_operands_untouched(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        assert evaluate(_bash("grep -rn X a.py", cwd=str(tmp_path)), allow_rewrite=True).rewrite is None

    def test_decide_never_rewrites(self):
        assert decide(_bash("grep -rn X .")) is None


class TestRecursiveTreeScan:
    """Without the rewrite channel, a tree holding a credential file is blocked."""

    def test_dotenv_in_tree_blocks(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".env").write_text("A=1\n")
        v = evaluate(_bash("grep -rn X .", cwd=str(tmp_path)))
        assert v.block and "sub/.env" in v.block and "--exclude=.env" in v.block

    def test_clean_tree_allows(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        assert evaluate(_bash("rg X .", cwd=str(tmp_path))) == (None, None, None)

    def test_pruned_dirs_are_skipped(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / ".env").write_text("A=1\n")
        assert evaluate(_bash("grep -r X .", cwd=str(tmp_path))).block is None

    def test_cd_moves_the_root(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / ".env").write_text("A=1\n")
        assert evaluate(_bash("cd app && grep -r X .", cwd=str(tmp_path))).block
        assert evaluate(_bash("grep -r X docs", cwd=str(tmp_path))).block is None

    def test_find_without_name_filter_scans(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert evaluate(_bash("find . -type f | xargs cat", cwd=str(tmp_path))).block
        assert evaluate(_bash("find . -name '*.py' | xargs cat", cwd=str(tmp_path))).block is None

    def test_decide_skips_the_walk(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert decide(_bash("grep -rn X .", cwd=str(tmp_path))) is None


class TestStandaloneLauncher:
    """The bundle runs this file by path as a second process."""

    GUARD = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "context" / "credential_guard.py")

    def _run(self, payload, tmp_path, env=None):
        import os
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, self.GUARD, "--deny-only"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": "", **(env or {})},
        )

    def test_explicit_read_exits_2(self, tmp_path):
        r = self._run(_bash("cat .env"), tmp_path)
        assert r.returncode == 2 and "BLOCKED" in r.stderr and r.stdout == ""

    def test_search_exits_0(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert self._run(_bash("grep -rn X .", cwd=str(tmp_path)), tmp_path).returncode == 0

    def test_switch_off(self, tmp_path):
        assert self._run(_bash("cat .env"), tmp_path, {"CREDENTIAL_GUARD_ENABLED": "false"}).returncode == 0

    def test_garbage_near_credential_exits_2(self, tmp_path):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, self.GUARD],
            input='{"tool_name": "Bash", "tool_input": {"command": "cat .env"',
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2 and "internal error" in r.stderr

    def test_garbage_elsewhere_exits_0(self, tmp_path):
        import subprocess
        import sys

        r = subprocess.run([sys.executable, self.GUARD], input="{not json", capture_output=True, text=True)
        assert r.returncode == 0


class TestRunsOnEveryTarget:
    """The whole point of the port: this reached Claude only before."""

    def test_copilot_normalized_payload_blocks(self):
        # what hooks.targets.normalizer produces for copilot's `bash` tool
        assert decide({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}) is not None

    def test_template_files_still_readable(self):
        assert decide({"tool_name": "Bash", "tool_input": {"command": "cat .env.example"}}) is None


PROC_ENVIRON_READS = [
    "tr '\\0' '\\n' < /proc/12345/environ | grep ^AGENTIHOOKS_SWARM",
    "cat /proc/self/environ",
    "cat /proc/$PID/environ",
    "strings /proc/1/environ",
    "grep -a REDIS /proc/1/environ",
    "xargs -0 -n1 -a /proc/1/environ echo",
    "ls /proc/*/environ | xargs cat",
    "python3 -c \"print(open('/proc/1/environ').read())\"",
    "python3 -c \"import os; print(open(os.path.join('/proc', '1', 'environ')).read())\"",
    "cd /proc/1 && cat environ",
    "while read -d '' l; do echo $l; done < /proc/thread-self/environ",
    "python3 <<'EOF'\nprint(open(f'/proc/{1}/environ').read())\nEOF",
    "cat /proc/1/envi*",
    "cat /proc/1/envir?n",
    "cat /proc/1/env''iron",
    "cat /proc/1/env\\iron",
    "f=environ; cat /proc/1/$f",
    "cat /proc/1/*",
    "cat /proc/1/env[i]ron",
    "a=envi; b=ron; cat /proc/1/$a$b",
    "cat /proc/1/$(printf envi)ron",
    "cat /proc/1/`printf envi`ron",
    "cd /proc/1 && cat e*",
    "cd /proc/1; cat *",
    "pushd /proc/self && cat env*",
    "cd -- /proc/1 && cat e*",
    "cd -L /proc/1 && cat e*",
    "cd /p*/1 && cat e*",
    "cd /p*c/1 && cat environ",
    "cat /p*c/1/environ|head",
    "cat /proc/1/envi*|tr '\\0' '\\n'",
    "cat /proc/1/env[i]ron;echo",
    "cat $(cat /pr?c/self/environ)",
    "cat /p*c/1/environ",
    "cat /pr?c/self/environ",
    "cat /pr[o]c/1/environ",
    "P=/pro; cat ${P}c/1/environ",
]

PROC_ALLOWED = [
    "cat /proc/meminfo",
    "python3 -c \"import os; print(os.listdir('/proc/1'))\"",
    'test -n "${REDIS_URL:-}" && echo set || echo unset',
    "ls /proc/1/status",
    "cat /proc/*/status",
    "test -d /proc/$pid && echo alive",
    "cd /home/x/procs && cat e*",
    "cd Xproc && cat environ",
    "cat build/environ",
]

PROC_ENVIRON_SHAPES = [
    "cat /*/self/environ",
    "cat /*/1/environ",
    "cat /proc/*/environ",
    "cat /proc/1/task/1/environ",
    "cat /proc/self/task/*/environ",
    "cat /p*/thread-self/env*",
    "cat /proc/self/fd/../environ",
    "cat /proc/1/./environ",
    "cat /proc/**/environ",
    "cat /proc/{1,self}/environ",
    "cat /proc/1/$(echo task)/1/environ",
    "cd /proc/1 && cat /proc/self/cwd/env*",
    "exec 3</proc/1 && cat /proc/self/fd/3/env*",
    "cat /proc/1/task/1/root/env*",
    "cat /proc/1/*/env*",
    "cat /*/self/cwd/env*",
    "cat /proc/1/task/$p",
    "cat /proc/self/fd/$p",
    "cat /proc/1/net/$p",
    "cat /host/proc/1/environ",
    "cat /mnt/host/proc/self/environ",
    "cd / && cat proc/1/environ",
    "cd / && cat */*/*/*/*",
    "cat $x/../environ",
    "cat file:///proc/1/environ",
]

GLOBS_SHAPED_UNLIKE_PROC_ENVIRON = [
    'case "$url" in https://github.com/*/*/issues/*) echo issue ;; esac',
    'case "$url" in https://github.com/*/*/pull/*) echo pull ;; esac',
    "ls src/*/tests/*",
    "ls tests/*/fixtures/*",
    "grep -rn TODO */src/e*",
    "ls docs/*/guide/*",
]


@pytest.mark.feature("credential-guard-proc-environ-path-shape")
@pytest.mark.parametrize("command", PROC_ENVIRON_SHAPES)
def test_a_proc_environ_path_shape_is_refused(command: str) -> None:
    assert decide(_bash(command)) == block("reading a process environment file under /proc", KIND_ENVIRONMENT)


@pytest.mark.feature("credential-guard-proc-environ-path-shape")
@pytest.mark.parametrize("command", GLOBS_SHAPED_UNLIKE_PROC_ENVIRON)
def test_a_glob_shaped_unlike_a_proc_environ_path_passes(command: str) -> None:
    assert decide(_bash(command)) is None


class TestProcEnvironReads:
    @pytest.mark.parametrize("command", PROC_ENVIRON_READS)
    def test_read_is_refused_with_the_leak_free_alternative(self, command):
        verdict = decide(_bash(command))
        assert verdict is not None
        assert verdict.splitlines()[0] == "BLOCKED: reading a process environment file under /proc."
        assert 'test -n "${VAR:-}"' in verdict
        assert ENV_NAMES_SAFE in verdict

    @pytest.mark.parametrize("command", PROC_ALLOWED)
    def test_neighbouring_commands_still_pass(self, command):
        assert decide(_bash(command)) is None

    @pytest.mark.parametrize("command", ["cat environ", "cat envi*", "cat $f", "cat *"])
    def test_read_from_a_proc_working_directory_is_refused(self, command):
        verdict = decide(_bash(command, cwd="/proc/1"))
        assert verdict is not None
        assert ENV_NAMES_SAFE in verdict

    def test_a_proc_working_directory_still_allows_other_files(self):
        assert decide(_bash("cat status", cwd="/proc/1")) is None
        assert decide(_bash("cat environ", cwd="/srv/procfiles")) is None

    @pytest.mark.parametrize("path", ["/proc/1/environ", "//proc/1/environ"])
    def test_read_tool_on_proc_environ_is_refused(self, path):
        verdict = decide({"tool_name": "Read", "tool_input": {"file_path": path}})
        assert verdict is not None
        assert ENV_NAMES_SAFE in verdict

    @pytest.mark.parametrize("path", ["docs/environ", "proc/1/environ", "/X/proc/1/environ"])
    def test_read_tool_on_an_environ_file_elsewhere_passes(self, path):
        assert decide({"tool_name": "Read", "tool_input": {"file_path": path}}) is None

    def test_grep_tool_on_proc_environ_is_refused(self):
        verdict = decide({"tool_name": "Grep", "tool_input": {"pattern": "REDIS", "path": "/proc/1/environ"}})
        assert verdict is not None
        assert verdict.splitlines()[0] == "BLOCKED: searching the contents of /proc/1/environ."
        assert 'test -n "${VAR:-}"' in verdict
        assert ENV_NAMES_SAFE in verdict


PROC_ENVIRON_BRACE_LEAVES = [
    "cat /proc/1/env{iron,}",
    "cat /proc/self/envi{ron,x}",
    "cat /proc/{1/envi,}ron",
    "cd /proc/1 && cat env{iron,}",
]


@pytest.mark.feature("credential-guard-refuses-brace-environ-leaves")
@pytest.mark.parametrize("command", PROC_ENVIRON_BRACE_LEAVES)
def test_a_brace_in_the_last_segment_of_a_proc_environ_path_is_refused(command: str) -> None:
    assert decide(_bash(command)) == block("reading a process environment file under /proc", KIND_ENVIRONMENT)
