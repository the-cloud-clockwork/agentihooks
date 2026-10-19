class Hooks:
    @property
    def config(self):
        from hooks import config

        return config

    @property
    def conditions(self):
        from hooks.context import conditions

        return conditions

    @property
    def filters(self):
        from hooks.filters import check

        return check

    @property
    def operator_words(self):
        from hooks.context import operator_words

        return operator_words

    @property
    def classifier(self):
        import hooks.classifier.code_rules
        import hooks.classifier.runner

        return hooks.classifier

    @property
    def profile_chain(self):
        from hooks.context import profile_chain

        return profile_chain

    def redis(self):
        from hooks._redis import get_redis

        return get_redis()
