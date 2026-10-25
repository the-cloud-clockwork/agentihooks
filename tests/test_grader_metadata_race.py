import json
import os
import subprocess
import time

import pytest

INTERPOSER = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void mark(const char *name) {
    int fd = open(getenv(name), O_CREAT | O_WRONLY, 0600);
    if (fd < 0) _exit(91);
    close(fd);
}

static void wait_for(const char *name) {
    for (int i = 0; i < 20000; i++) {
        if (access(getenv(name), F_OK) == 0) return;
        usleep(1000);
    }
    _exit(92);
}

int mkdir(const char *path, mode_t mode) {
    int (*real_mkdir)(const char *, mode_t) = dlsym(RTLD_NEXT, "mkdir");
    int result = real_mkdir(path, mode);
    if (!result && strstr(path, ".git/worktrees/grader")) {
        fprintf(stderr, "PROBE pid=%d mkdir=%s locked=absent\n", getpid(), path);
        mark("PROBE_CREATED");
        wait_for("PROBE_RELEASE");
    }
    return result;
}

DIR *opendir(const char *path) {
    DIR *(*real_opendir)(const char *) = dlsym(RTLD_NEXT, "opendir");
    char command[1024] = {0};
    int fd = open("/proc/self/cmdline", O_RDONLY);
    ssize_t n = read(fd, command, sizeof(command));
    close(fd);
    if (n > 0 && memmem(command, n, "\0maintenance\0", 13) && strstr(path, "worktrees")) {
        fprintf(stderr, "PROBE pid=%d maintenance opendir=%s\n", getpid(), path);
        mark("PROBE_MAINTENANCE");
        wait_for("PROBE_CREATED");
    }
    return real_opendir(path);
}
"""


def wait_until(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < deadline, "probe synchronization timed out"
        time.sleep(0.01)


@pytest.mark.parametrize("disable_maintenance", [False, True, "environment"])
def test_fetch_maintenance_removes_initializing_grader(tmp_path, disable_maintenance):
    def git(*args, **kwargs):
        return subprocess.run(
            ["git", *args], cwd=repo, env=environment, text=True, capture_output=True, check=True, **kwargs
        )

    repo = tmp_path / "repository"
    repo.mkdir()
    environment = dict(os.environ)
    environment.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    version = git("--version").stdout.strip()
    print(version, flush=True)
    assert tuple(map(int, version.split()[-1].split(".")[:2])) >= (2, 54)
    git("init")
    git(
        "-c",
        "maintenance.auto=false",
        "-c",
        "user.name=Probe",
        "-c",
        "user.email=probe@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "seed",
    )
    git("config", "gc.auto", "0")
    git("remote", "add", "origin", str(repo))
    if disable_maintenance == "environment":
        environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="maintenance.auto", GIT_CONFIG_VALUE_0="false")
    elif disable_maintenance:
        git("config", "maintenance.auto", "false")
    print("config", git("config", "--get-regexp", "^(gc|maintenance)\\.").stdout, flush=True)
    source = tmp_path / "pause.c"
    library = tmp_path / "pause.so"
    source.write_text(INTERPOSER)
    compiled = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "gcc:14",
            "sh",
            "-c",
            "gcc -shared -fPIC -x c - -o /tmp/probe.so -ldl && cat /tmp/probe.so",
        ],
        input=source.read_bytes(),
        capture_output=True,
        check=True,
    )
    library.write_bytes(compiled.stdout)
    created = tmp_path / "created"
    release = tmp_path / "release"
    maintenance = tmp_path / "maintenance"
    trace = tmp_path / "trace.jsonl"
    environment.update(
        LD_PRELOAD=str(library),
        PROBE_CREATED=str(created),
        PROBE_RELEASE=str(release),
        PROBE_MAINTENANCE=str(maintenance),
        GIT_TRACE2_EVENT=str(trace),
    )
    destination = tmp_path / "grader"
    metadata = repo / ".git" / "worktrees" / "grader"
    fetch = subprocess.Popen(
        ["git", "fetch", "--no-tags", "origin", "HEAD"],
        cwd=repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not disable_maintenance:
        wait_until(maintenance.exists)
    add = subprocess.Popen(
        ["git", "worktree", "add", "--detach", str(destination), "HEAD"],
        cwd=repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until(created.exists)
        print("metadata created; locked and gitdir absent", flush=True)
        if disable_maintenance:
            fetch_output = fetch.communicate(timeout=20)
            assert metadata.is_dir()
            assert not maintenance.exists()
        else:
            wait_until(lambda: not metadata.exists())
            print("metadata removed by fetch maintenance before lock creation", flush=True)
        release.touch()
        add_output = add.communicate(timeout=20)
        fetch_output = fetch.communicate(timeout=20)
    finally:
        release.touch()
        for name, process in (("add", add), ("fetch", fetch)):
            if process.poll() is None:
                process.kill()
            print("cleanup", name, process.returncode, *process.communicate(), flush=True)
    print("fetch", fetch.returncode, *fetch_output, flush=True)
    print("worktree", add.returncode, *add_output, flush=True)
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    commands = [event["argv"] for event in events if event["event"] in {"start", "child_start"}]
    print("trace commands", json.dumps(commands), flush=True)
    assert fetch.returncode == 0
    if disable_maintenance:
        assert add.returncode == 0
        assert (metadata / "gitdir").is_file()
        assert not any(command[1:3] == ["worktree", "prune"] for command in commands)
    else:
        assert add.returncode == 128
        assert ".git/worktrees/grader/locked" in add_output[1]
        assert "No such file or directory" in add_output[1]
        assert any(command[1:3] == ["worktree", "prune"] for command in commands)
        pytest.fail("Fetch maintenance pruned initializing grader metadata despite gc.auto=0")
