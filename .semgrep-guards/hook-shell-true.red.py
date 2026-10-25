import asyncio
import os
import subprocess


def run(command):
    # ruleid: hook-shell-true
    subprocess.run(command, shell=True, check=False)
    # ruleid: hook-shell-true
    os.system(command)
    # ruleid: hook-shell-true
    return os.popen(command).read()


async def spawn(command):
    # ruleid: hook-shell-true
    return await asyncio.create_subprocess_shell(command)
