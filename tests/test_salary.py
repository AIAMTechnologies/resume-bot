import pytest

from resumebot.engine.salary import below_floor, posted_max


@pytest.mark.parametrize("text,top", [
    ("The pay range is $50,000 to $65,000 USD. Plus a $500 home office stipend and $75 USD monthly.", 65_000),
    ("Annual Base Salary Range: $169,000 - $245,000 USD", 245_000),
    ("Compensation: $154,000 – $205,000 USD", 205_000),
    ("Base salary $120k-$150k", 150_000),
    ("Hourly rate: $45 - $60 per hour", 60 * 2080),
    ("We offer a $1,000 learning budget.", None),
    ("No pay information here.", None),
])
def test_posted_max(text, top):
    assert posted_max(text) == top


def test_floor():
    assert below_floor("", "The pay range is $50,000 to $65,000", 100_000) == (True, 65_000)
    assert below_floor("", "Range $90,000 - $120,000", 100_000) == (False, 120_000)  # top clears the floor
    assert below_floor("", "No pay posted", 100_000) == (False, None)                # unknown pay passes
