import asyncio
import subprocess


def run(args):
    subprocess.run(args, shell=False, check=False)
    return subprocess.run(["git", "status"], capture_output=True)


async def spawn(args):
    return await asyncio.create_subprocess_exec(*args)
