import copy
import subprocess
import sys

import yaml


def check(text, mode):
    objects = list(yaml.safe_load_all(text))
    worker = next(
        obj
        for obj in objects
        if obj and obj.get("kind") == "Deployment" and obj["metadata"]["name"].endswith("worker-hive")
    )
    env = {row["name"]: row for row in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HIVE_GITHUB_AUTH_MODE"]["value"] == mode
    fields = (
        {"GITHUB_TOKEN": "pat"}
        if mode == "token"
        else {"GITHUB_APP_ID": "id", "GITHUB_APP_INSTALLATION_ID": "inst", "GITHUB_APP_PRIVATE_KEY": "pem"}
    )
    selected = {key: row["valueFrom"]["secretKeyRef"] for key, row in env.items() if key.startswith("GITHUB_")}
    assert selected == {key: {"name": "github-access", "key": value} for key, value in fields.items()}
    return objects


def main():
    common = [
        "helm",
        "template",
        "fixture",
        sys.argv[1],
        "--set",
        "workers.replicas=1",
        "--set",
        "workers.credentialSecret.name=member",
        "--set",
        "workers.credentialSecret.key=credential",
        "--set",
        "workers.githubAuth.secretName=github-access",
        "--set",
        "workers.githubAuth.tokenKey=pat",
        "--set",
        "workers.githubAuth.appIdKey=id",
        "--set",
        "workers.githubAuth.installationIdKey=inst",
        "--set",
        "workers.githubAuth.privateKeyKey=pem",
    ]
    for mode in ("token", "app"):
        result = subprocess.run(
            [*common, "--set", f"workers.githubAuth.mode={mode}"], capture_output=True, text=True, check=True
        )
        objects = check(result.stdout, mode)
        planted = copy.deepcopy(objects)
        worker = next(
            obj
            for obj in planted
            if obj and obj.get("kind") == "Deployment" and obj["metadata"]["name"].endswith("worker-hive")
        )
        env = worker["spec"]["template"]["spec"]["containers"][0]["env"]
        row = next(row for row in env if row["name"].startswith("GITHUB_"))
        row["valueFrom"]["secretKeyRef"]["name"] = "wrong-secret"
        try:
            check(yaml.safe_dump_all(planted), mode)
        except AssertionError:
            pass
        else:
            raise AssertionError("A wrong GitHub Secret escaped the chart proof")
    result = subprocess.run([*common, "--set", "workers.githubAuth.mode=other"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "workers.githubAuth.mode must be token or app" in result.stderr
    print("GitHub chart authentication: two modes pass, two wrong Secret plants fail, unknown mode refused")


if __name__ == "__main__":
    main()
