def broad(store, args):
    # ruleid: learned-default-all-seats
    _list_learned(store, args.slug)


def scoped(store, args, seat):
    # ok: learned-default-all-seats
    _list_learned(store, args.slug, seat, all_seats=args.all)
