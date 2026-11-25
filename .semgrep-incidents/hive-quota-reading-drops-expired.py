def dropped(reading: dict) -> object:
    try:
        # ruleid: hive-quota-reading-drops-expired
        five, week = reading["five_hour"], reading["seven_day"]
    except KeyError:
        return None
    return observe(five, week)


def kept(reading: dict) -> object:
    try:
        # ok: hive-quota-reading-drops-expired
        five, week = reading["five_hour"], reading["seven_day"]
    except KeyError:
        return None
    return expire(observe(five, week)) if reading.get("expired") else observe(five, week)
