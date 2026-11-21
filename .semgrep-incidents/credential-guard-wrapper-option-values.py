def verb_index(tokens: list[str]) -> int:
    for i, tok in enumerate(tokens):
        if tok == "sudo":
            continue
        # ruleid: credential-guard-wrapper-option-values
        if tok.startswith("-") and i > 0 and tokens[i - 1] == "sudo":
            continue
        return i
    return len(tokens)


def clean_wrapper(tokens: list[str]) -> int:
    def verb_index(tokens: list[str]) -> int:
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            option = tokens[i]
            i += 1
            # ok: credential-guard-wrapper-option-values
            if option in ("-u", "--user") and i < len(tokens):
                i += 1
        return i

    return verb_index(tokens)
