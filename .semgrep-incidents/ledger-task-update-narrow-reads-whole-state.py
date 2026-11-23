def apply_claim(services, slug):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return services.repository.current(slug).state


def hook_state(services, slug):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return services.repository.get_document(slug)


def update(repository, connection, slug):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return repository._entry(connection, slug)


def persist(entry):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return entry.for_write()


def placed(connection, slug):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return load(connection, slug)


def admitted(connection, slug):
    # ruleid: ledger-task-update-narrow-reads-whole-state
    return read_rows(connection, slug)


class Keyed:
    def apply_claim(self, services, slug):
        # ok: ledger-task-update-narrow-reads-whole-state
        return services.repository.read(slug, "_meta.warnings")

    def update(self, connection, slug, keys):
        # ok: ledger-task-update-narrow-reads-whole-state
        return partial_rows(connection, slug, keys)


def apply(services, slug):
    # ok: ledger-task-update-narrow-reads-whole-state
    return services.repository.current(slug).state
