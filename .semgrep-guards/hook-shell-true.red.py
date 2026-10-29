import asyncio
import os
import subprocess
import subprocess as sp
from subprocess import run


def shell(command, use_shell):
    # ruleid: hook-shell-true
    subprocess.run(command, shell=True, check=False)
    # ruleid: hook-shell-true
    sp.Popen(command, shell=use_shell)
    # ruleid: hook-shell-true
    run(command, shell=True)
    # ruleid: hook-shell-true
    subprocess.run(["bash", "-c", command])
    # ruleid: hook-shell-true
    subprocess.check_output(("/bin/sh", "-c", command))
    # ruleid: hook-shell-true
    subprocess.getoutput(command)
    # ruleid: hook-shell-true
    subprocess.getstatusoutput(command)
    # ruleid: hook-shell-true
    os.system(command)
    # ruleid: hook-shell-true
    return os.popen(command).read()


async def spawn(command):
    # ruleid: hook-shell-true
    return await asyncio.create_subprocess_shell(command)
