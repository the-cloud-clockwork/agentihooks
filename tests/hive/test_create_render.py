import pytest

from scripts.hive import auth, create_render

pytestmark = pytest.mark.unit

HIVE = create_render.Hive(
    "swarm-hive-1800000000", "k8s/agentihooks-hive-swarm-hive-1800000000", 2225, "worker-002", "10.10.30.207"
)
TEMPLATE = """# header lines before the first file are dropped
#@ file: k8s/argocd/apps/${name}.yaml
name: ${name}
values: $$values/k8s/values-overlays/${name}/values.yaml
#@ file: k8s/manifests/${name}/hive.yaml
key: ${record}
port: ${ssh_port}
node: ${node}
ip: ${lan_ip}
"""


def test_render_splits_the_template_into_its_named_files_with_every_placeholder_filled():
    assert create_render.render(TEMPLATE, HIVE) == {
        "k8s/argocd/apps/swarm-hive-1800000000.yaml": (
            "name: swarm-hive-1800000000\nvalues: $values/k8s/values-overlays/swarm-hive-1800000000/values.yaml\n"
        ),
        "k8s/manifests/swarm-hive-1800000000/hive.yaml": (
            "key: k8s/agentihooks-hive-swarm-hive-1800000000\nport: 2225\nnode: worker-002\nip: 10.10.30.207\n"
        ),
    }


@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("#@ file: a.yaml\nx: ${unknown}\n", "unknown placeholder 'unknown'"),
        ("#@ file: a.yaml\nx: $\n", "malformed placeholder"),
        ("x: ${name}\n", "names no file with a #@ file: line"),
    ],
)
def test_render_refuses_a_template_it_cannot_fill_or_split(template, reason):
    with pytest.raises(auth.HiveError, match=reason):
        create_render.render(template, HIVE)


def _service(ip, *ports):
    annotations = {create_render.LAN_IP_ANNOTATION: ip} if ip else {}
    return {"metadata": {"annotations": annotations}, "spec": {"ports": [{"port": port} for port in ports]}}


def test_next_port_is_the_first_port_from_2222_free_on_the_lan_address():
    services = [
        _service("10.10.30.207", 2222),
        _service("10.10.30.207", 2223, 8765),
        _service("10.10.30.208,10.10.30.207", 2224),
        _service("10.10.30.2070", 2225),
        _service("10.10.30.208", 2225),
        _service(None, 2225),
        {"metadata": {}},
    ]
    assert create_render.next_port(services, "10.10.30.207") == 2225


def test_next_port_fills_a_gap():
    assert create_render.next_port([_service("10.10.30.207", 2222, 2224)], "10.10.30.207") == 2223


def _shared(ip, key=create_render.SHARED_IP_KEY):
    service = _service(ip, 22)
    service["metadata"]["annotations"][create_render.SHARED_IP_ANNOTATION] = key
    return service


def test_the_lan_address_is_the_one_the_swarm_services_share():
    services = [_shared("10.10.30.207"), _shared("10.10.30.207"), _shared("10.9.9.9", "other"), _service("10.8.8.8", 1)]
    assert create_render.lan_address([*services, _shared(None)]) == "10.10.30.207"


@pytest.mark.parametrize(
    ("services", "named"),
    [([], "0 addresses ()"), ([_shared("10.1.1.1"), _shared("10.2.2.2")], "2 addresses (10.1.1.1, 10.2.2.2)")],
)
def test_no_single_shared_lan_address_is_a_refusal(services, named):
    with pytest.raises(auth.HiveError) as refused:
        create_render.lan_address(services)
    assert str(refused.value) == f"the agentihooks-swarm LAN services name {named}; expected one"
