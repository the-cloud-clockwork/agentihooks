import json
import os
import tempfile
from pathlib import Path

MIB = 1024 * 1024
SIZE = 1024 * MIB
TOLERANCE = 64 * MIB


def filesystem(path: Path) -> dict:
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ")
        fields, source = left.split(), right.split()
        mount = Path(fields[4])
        if path == mount or mount in path.parents:
            mounts.append(
                (len(mount.parts), {"device": fields[2], "mount": str(mount), "type": source[0], "source": source[1]})
            )
    result = max(mounts, key=lambda item: item[0])[1]
    if result["device"].startswith("0:") or not result["source"].startswith("/dev/"):
        raise RuntimeError(f"{path} is not on a block device: {result}")
    return result


def memory() -> dict:
    info = dict(line.split(":") for line in Path("/proc/meminfo").read_text().splitlines())
    values = {key: int(value.split()[0]) * 1024 for key, value in info.items()}
    return {
        "used": values["MemTotal"] - values["MemAvailable"],
        "anonymous": values["AnonPages"],
        "shmem": values["Shmem"],
    }


def main() -> None:
    chunk = bytes(MIB)
    baseline = memory()
    print(json.dumps({"baseline": baseline}))
    for folder in (Path("/home/worker"), Path("/tmp")):
        print(json.dumps({"path": str(folder), "filesystem": filesystem(folder)}))
        with tempfile.TemporaryFile(dir=folder, buffering=0) as stream:
            peak = baseline.copy()
            for count in range(SIZE // MIB):
                stream.write(chunk)
                if (count + 1) % 16 == 0:
                    os.fsync(stream.fileno())
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                    sample = memory()
                    peak = {key: max(peak[key], value) for key, value in sample.items()}
            after = memory()
            peak = {key: max(peak[key], value) for key, value in after.items()}
            result = {
                "path": str(folder),
                "bytes": os.fstat(stream.fileno()).st_size,
                "before": baseline,
                "after": after,
                "peak": peak,
            }
            print(json.dumps(result))
            if result["bytes"] != SIZE or any(peak[key] - baseline[key] > TOLERANCE for key in baseline):
                raise RuntimeError("large file write increased guest memory beyond 64 MiB")
    print("storage proof passed")


if __name__ == "__main__":
    main()
