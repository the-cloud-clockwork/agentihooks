"""Complete session evidence comes from catalog rows and verified archive objects; a preview is never complete."""

LEGACY_PREVIEW_CHARS = 0
MISSING = ""
ALTERED = ""
UNCOVERED = ""
FORBIDDEN = ""
UNAVAILABLE = ""


class EvidenceIntegrityError(Exception):
    pass


class EvidenceRefused(Exception):
    pass


class ObjectStore:
    def __init__(self, root):
        raise NotImplementedError


def evidence(catalog, store, grant, domain, session_uid):
    raise NotImplementedError


def source_range(catalog, store, grant, reference):
    raise NotImplementedError


def preview(found, limit=LEGACY_PREVIEW_CHARS):
    raise NotImplementedError
