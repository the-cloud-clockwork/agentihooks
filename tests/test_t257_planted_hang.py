import threading


def test_t257_planted_hang_waits_past_the_faulthandler_timeout():
    threading.Event().wait(75)
