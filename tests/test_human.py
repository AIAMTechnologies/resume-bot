import math
import random

from resumebot.browser.human import bezier_path, key_delays, reading_seconds


def test_bezier_lands_exactly_on_target_and_curves():
    rng = random.Random(3)
    path = bezier_path((10, 10), (800, 500), rng)
    assert path[-1] == (800, 500) or math.dist(path[-1], (800, 500)) < 1e-6
    assert len(path) >= 12
    # Not a straight line: some point deviates from the direct segment.
    def off_line(p):
        (x0, y0), (x1, y1) = (10, 10), (800, 500)
        return abs((y1 - y0) * p[0] - (x1 - x0) * p[1] + x1 * y0 - y1 * x0) / math.dist((x0, y0), (x1, y1))
    assert max(off_line(p) for p in path) > 5


def test_key_delays_match_wpm_roughly():
    rng = random.Random(4)
    text = "Designed and shipped a data pipeline in Python. " * 3
    delays = key_delays(text, (60, 60), rng)
    assert len(delays) == len(text) and all(0 < d <= 2.5 for d in delays)
    wpm = (len(text) / 5) / (sum(delays) / 60)
    assert 25 < wpm < 80  # pauses slow it down below the raw rate, but stays human


def test_reading_time_capped_and_scales():
    rng = random.Random(5)
    short = reading_seconds("word " * 50, (250, 250), rng=rng)
    long = reading_seconds("word " * 5000, (250, 250), rng=rng)
    assert short < long <= 90
