from requests import Response

from scripts.github_app import REFUSED, GitAuthRefused


def missing(response: Response) -> None:
    if response.status_code != 201:
        # ruleid: github-app-mint-loses-status
        raise GitAuthRefused(REFUSED)


def discarded(response: Response) -> None:
    if response.status_code != 201:
        # ruleid: github-app-mint-loses-status
        raise GitAuthRefused(REFUSED, status_code=None)


def preserved(response: Response) -> None:
    if response.status_code != 201:
        # ok: github-app-mint-loses-status
        raise GitAuthRefused(REFUSED, status_code=response.status_code)


def positional(response: Response) -> None:
    if response.status_code != 201:
        # ok: github-app-mint-loses-status
        raise GitAuthRefused(REFUSED, response.status_code)
