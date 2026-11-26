import os
import stat
from pathlib import Path

OWNED = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR


def changes_the_mode_of_every_folder_it_empties(folder: Path) -> None:
    # ruleid: lab-empty-chmods-folder-unconditionally
    folder.chmod(folder.stat().st_mode | OWNED)
    for entry in folder.iterdir():
        entry.unlink()


def changes_the_mode_through_os_of_every_folder_it_empties(folder: Path) -> None:
    # ruleid: lab-empty-chmods-folder-unconditionally
    os.chmod(folder, folder.stat().st_mode | OWNED)
    for entry in folder.iterdir():
        entry.unlink()


def changes_the_mode_only_of_a_folder_that_lacks_an_owner_bit(folder: Path) -> None:
    mode = folder.stat().st_mode
    if mode & OWNED != OWNED:
        # ok: lab-empty-chmods-folder-unconditionally
        folder.chmod(mode | OWNED)
    for entry in folder.iterdir():
        entry.unlink()


def seals_each_entry_of_a_folder(folder: Path) -> None:
    for entry in folder.iterdir():
        # ok: lab-empty-chmods-folder-unconditionally
        entry.chmod(entry.stat().st_mode & ~stat.S_IWUSR)
