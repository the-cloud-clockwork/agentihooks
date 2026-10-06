class ClassifierError(Exception):
    pass


class ClassifierInputError(ClassifierError):
    pass


class ClassifierRequestError(ClassifierError):
    pass


class ClassifierUnavailable(ClassifierError):
    pass


class BackendFailure(ClassifierError):
    def __init__(self, reason: str, *, skip_api: bool = False):
        super().__init__(reason)
        self.skip_api = skip_api
